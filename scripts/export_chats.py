"""Выгрузка переписки с Claude Code в архив для письма.

Копирует JSONL сессий (и вложенные <id>/subagents/*.jsonl), скрывает данные по файлу замен,
проверяет результат на секреты и собирает zip с ai-chats/ внутри.

Запуск:
    uv run python scripts/export_chats.py --replace ~/Projects/skytecgames-redact.txt \
        --session d6eda45b-40da-4b90-b663-950e550c0f8d=00-plan \
        --session 42edc078-52d8-42f7-94c4-bab63710ae43=01-skeleton ...

Файл замен — формат git-filter-repo --replace-text: "literal==>замена", "regex:...==>замена",
строка без "==>" заменяется на "***REMOVED***". Файл замен и найденные значения не печатаются.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

SRC = Path.home() / ".claude/projects/-Users-aleksej-dzyuba-Projects-skytecgames"
OUT = Path.home() / "Projects/skytecgames-ai-chats.zip"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

FAKE_KEY = "sk-ant-api03-test-DO-NOT-LEAK"
KEY_SHAPE = re.compile(r"sk-ant-api\d\d-[A-Za-z0-9_\-]{20,}")
ENV_KEY_LINE = re.compile(r"ANTHROPIC_API_KEY=(?!\[REDACTED\])[^\s\"'\\`]+")
# Значения из .env, которые проверяем. Публичный ключ фронтенда Metacritic лежит в app/config.py.
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASS", re.I)
PUBLIC_NAMES = {"METACRITIC_API_KEY"}
# Публичный web-ключ Firebase со страниц metacritic.com — не наш, но тоже скрываем.
GCP_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
TOKEN = re.compile(r"[A-Za-z0-9_\-+/=.:]{8,}")

README = """# Переписка с Claude Code

Сырые логи сессий Claude Code (JSONL: одна строка — одно событие: сообщение пользователя,
ответ модели, вызов инструмента или его результат). Личные данные и секреты заменены на
`[скрыто]` / `[REDACTED]`.

## Как читать

Только текст сообщений человека и модели:

```bash
jq -r 'select(.type=="user" or .type=="assistant")
  | .message.content
  | if type=="string" then . else (map(select(.type=="text") | .text) | join("\\n")) end
  | select(length>0)' 01-skeleton.jsonl | less
```

Какие инструменты вызывались:

```bash
jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .name' \\
  06-web.jsonl | sort | uniq -c | sort -rn
```

Вложенные папки `<имя>/subagents/` — логи субагентов, которых запускала эта сессия.

## Сессии

{sessions}
"""

DESCRIPTIONS = {
    "00-plan": "Планирование: разбор ТЗ, выбор стека, PLAN.md, запуск и приёмка сессий шагов",
    "01-skeleton": "Шаг 1. Каркас проекта: uv, FastAPI, SQLAlchemy, Docker, pre-commit",
    "02-scraper": "Шаг 2. Сборщик Metacritic: внутренний JSON API, отбор 20 игр в час",
    "03-llm": "Шаг 3. Резюме отзывов через Claude Haiku 5.5, бюджет $3 в сутки",
    "04-similar": "Шаг 4. Похожие игры: эмбеддинги bge-small через fastembed",
    "05-worker": "Шаг 5. Планировщик APScheduler и воркер прогона",
    "06-web": "Шаг 6. Веб-интерфейс: Jinja2 + HTMX, список, фильтры, карточка игры",
    "07-monitor": "Шаг 7. Мониторинг прогонов (SSE) и кнопка ручного запуска",
    "08-letsplay": "Шаг 8. Летсплеи YouTube через yt-dlp и резюме субтитров",
    "09-security": "Шаг 9. Тесты и аудит безопасности",
    "10-deploy": "Шаг 10. Деплой на VPS: Docker Compose, nginx, certbot, Basic Auth",
    "11-docs": "Шаг 11. README, выгрузка переписки, черновик письма",
}


@dataclass
class Rule:
    pattern: re.Pattern[str]
    repl: str
    literal: str | None  # исходная строка для literal-правил


def load_rules(path: Path) -> list[Rule]:
    rules = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        src, sep, repl = raw.partition("==>")
        if not sep:
            repl = "***REMOVED***"
        if src.startswith("regex:"):
            rules.append(Rule(re.compile(src[6:]), repl, None))
        else:
            if src.startswith("literal:"):
                src = src[8:]
            variants = {src, json.dumps(src)[1:-1], json.dumps(src, ensure_ascii=False)[1:-1]}
            alt = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
            rules.append(Rule(re.compile(alt), repl, src))
    return rules


def load_env_secrets() -> list[str]:
    """Значения секретных переменных .env. Не печатаются, наружу идут только хэши."""
    values = []
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip().strip("\"'")
        if not sep or name.startswith("#") or name in PUBLIC_NAMES:
            continue
        if SECRET_NAME.search(name) and len(value) >= 8:
            values.append(value)
    return values


def redact(text: str, rules: list[Rule], env_values: list[str]) -> str:
    for rule in rules:
        text = rule.pattern.sub(lambda _m, r=rule.repl: r, text)
    text = ENV_KEY_LINE.sub("ANTHROPIC_API_KEY=[REDACTED]", text)
    text = GCP_KEY.sub("[REDACTED]", text)
    text = KEY_SHAPE.sub(lambda m: m[0] if m[0].startswith(FAKE_KEY) else "[REDACTED]", text)
    for value in env_values:
        text = text.replace(value, "[REDACTED]")
    return text


def public_secrets() -> set[str]:
    """Публичный ключ фронтенда Metacritic: он лежит в app/config.py открытого репозитория."""
    config = (ENV_FILE.parent / "app/config.py").read_text(encoding="utf-8")
    return set(re.findall(r'metacritic_api_key: str = "([^"]+)"', config))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def check_dir(root: Path, rules: list[Rule], env_values: list[str]) -> list[str]:
    """Проверки результата. Возвращает список проблем без самих значений."""
    problems = []
    env_hashes = {sha(v) for v in env_values}
    for f in sorted(root.rglob("*.jsonl")):
        rel = f.relative_to(root)
        text = f.read_text(encoding="utf-8")
        bad_json = 0
        for line in text.splitlines():
            if line.strip():
                try:
                    json.loads(line)
                except ValueError:
                    bad_json += 1
        if bad_json:
            problems.append(f"{rel}: {bad_json} строк не разбираются как JSON")
        keys = [m for m in KEY_SHAPE.findall(text) if not m.startswith(FAKE_KEY)]
        if keys:
            problems.append(f"{rel}: {len(keys)} ключей вида sk-ant-api")
        mentions = text.count("sk-ant-api") - text.count(FAKE_KEY)
        if mentions:
            print(f"  {rel}: упоминаний 'sk-ant-api' без ключа: {mentions} (команды поиска)")
        hits = sum(1 for t in TOKEN.findall(text) if sha(t) in env_hashes)
        hits += sum(text.count(v) for v in env_values)
        if hits:
            problems.append(f"{rel}: {hits} совпадений со значениями из .env")
        for i, rule in enumerate(rules, 1):
            n = len(rule.pattern.findall(text))
            if n:
                problems.append(f"{rel}: правило замен №{i} — {n} совпадений")
    report = root.parent / "gitleaks.json"
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        return [*problems, "gitleaks не установлен (brew install gitleaks)"]
    proc = subprocess.run(  # noqa: S603 — аргументы наши, без shell
        [
            gitleaks,
            "detect",
            "--no-git",
            "--source",
            str(root),
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        problems.append(f"gitleaks не запустился: {proc.stderr.strip()[:200]}")
    elif report.exists():
        found: dict[str, int] = {}
        allowed = public_secrets()
        for leak in json.loads(report.read_text() or "[]"):
            if leak["Secret"] in allowed:
                continue
            rel = Path(leak["File"]).resolve().relative_to(root.resolve())
            where = f"{rel} правило {leak['RuleID']}"
            found[where] = found.get(where, 0) + 1
        problems += [f"gitleaks: {k} — {n} находок" for k, n in found.items()]
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--replace", type=Path, required=True, help="файл замен (вне репозитория)")
    ap.add_argument("--session", action="append", required=True, metavar="ID=ИМЯ")
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    rules = load_rules(args.replace.expanduser())
    env_values = load_env_secrets()
    sessions = [s.split("=", 1) for s in args.session]
    print(f"Правил замен: {len(rules)}, секретов из .env: {len(env_values)}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ai-chats"
        root.mkdir()
        lines = []
        for sid, name in sessions:
            sources = [(args.src / f"{sid}.jsonl", root / f"{name}.jsonl")]
            for sub in sorted((args.src / sid / "subagents").glob("*.jsonl")):
                sources.append((sub, root / name / "subagents" / sub.name))
            for src, dst in sources:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(
                    redact(src.read_text(encoding="utf-8"), rules, env_values), encoding="utf-8"
                )
            subs = len(sources) - 1
            extra = f" (+ субагентов: {subs}, папка `{name}/subagents/`)" if subs else ""
            lines.append(f"- `{name}.jsonl` — {DESCRIPTIONS.get(name, '')}{extra}")
            print(f"  {sid}.jsonl -> {name}.jsonl, субагентов {subs}")
        (root / "README.md").write_text(
            README.replace("{sessions}", "\n".join(lines)), encoding="utf-8"
        )

        problems = check_dir(root, rules, env_values)
        if problems:
            print("ПРОВЕРКА НЕ ПРОЙДЕНА, архив не собран:")
            print("\n".join(f"  {p}" for p in problems))
            return 1

        args.out.unlink(missing_ok=True)
        with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(root.parent))
        print(f"Архив: {args.out} ({args.out.stat().st_size // 1024} КБ)")

    # Повторная проверка уже собранного архива.
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(args.out) as zf:
            zf.extractall(tmp)
        problems = check_dir(Path(tmp) / "ai-chats", rules, env_values)
        if problems:
            print("АРХИВ НЕ ПРОШЁЛ ПРОВЕРКУ, удаляю:")
            print("\n".join(f"  {p}" for p in problems))
            args.out.unlink()
            return 1
    print("Проверка архива: 0 совпадений с файлом замен, .env и gitleaks")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# claude-skills

Skills para [Claude Code](https://claude.com/claude-code). Cada carpeta es una skill autocontenida
con su `SKILL.md` y sus templates.

## Skills

| Skill | Qué hace |
|---|---|
| [`telemetria-axiom/`](telemetria-axiom/) | Telemetría OpenTelemetry → Axiom (logs + traces vía OTLP, sin Collector). Templates para Python, Node.js/TS, C# y Rust. |

## Instalar

Clonar y enlazar la skill que quieras a tu carpeta de skills de Claude Code:

```bash
git clone https://github.com/jcarias93/claude-skills.git
ln -s "$PWD/claude-skills/telemetria-axiom" ~/.claude/skills/telemetria-axiom
```

O copiarla, si preferís no depender del clon:

```bash
cp -R claude-skills/telemetria-axiom ~/.claude/skills/
```

Para una skill de proyecto en vez de personal, usá `.claude/skills/` dentro del repo.

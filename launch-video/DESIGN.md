# Handle — Launch Video Visual Identity

## Style Prompt
Dark, technical, calm-then-confident. A product launch for a developer tool: deep ink canvas, a single luminous teal as the hero accent, warm amber as a sparing secondary. Clean monospace and a crisp geometric sans. Motion is precise and purposeful — chips pop, a scan beam sweeps, a packet of content flows from browser to agent. No clutter, no neon overload; restraint that reads as engineering quality.

## Colors
- `#0a0f14` — ink / canvas (background)
- `#0e1620` / `#11191f` — panel surfaces (browser, terminal, cards)
- `#1f2c36` — lines / borders
- `#4fe0b0` — mint (hero accent: handles, beams, success, brand)
- `#86f0d0` — mint-soft (highlights)
- `#f4b860` — amber (sparing secondary highlight)
- `#e8edf0` — ink-light (primary text)
- `#94a3ad` — ink-soft (secondary text)
- `#ff6b6b` — danger (the "can't read" / blind state, used once)

## Typography
- `Space Grotesk` — headlines, UI labels
- `JetBrains Mono` — handles (t1, t2), terminal, commands, code

## Motion
- Precise eases: power3.out / expo.out for entrances, back.out for chip pops.
- Scene changes = opacity crossfade (z-stacked opaque scenes), 0.6s.
- One signature move per scene (beam sweep, packet flow), not many competing.

## What NOT to Do
- No generic blue (#3b82f6), no Roboto/Inter for display, no pure-white #fff text on dark (use #e8edf0).
- No full-screen linear gradients (banding) — radial/solid + localized mint glow only.
- No more than one dark-teal + one amber on screen at once; mint is the star.
- No exit animations except the final scene; transitions handle scene changes.

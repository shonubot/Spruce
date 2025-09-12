
# Spruce (io.github.shonubot.Spruce)

<br>Spruce allows you to safely clean app caches which can free storage space and even improve the stability of some apps.

## Stack
- Python 3 + GTK 4 + libadwaita
- Blueprint (.blp) for UI
- Flatpak manifest provided

## Run (bare)
```bash
PYTHONPATH=src python3 -m spruce.app
```

## Build (Flatpak)
```bash
flatpak-builder build-dir io.github.shonubot.Spruce.json --install --user
flatpak run io.github.shonubot.Spruce
```

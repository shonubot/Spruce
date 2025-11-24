![Spruce logo](data/icons/hicolor/scalable/apps/io.github.shonubot.Spruce.svg)
### **Spruce**
**Spruce** is a lightweight cache cleaner and system maintenance tool designed for **GNU/Linux**.
It helps keep your system fresh by clearing unneeded caches, logs, temporary files and unused Flatpak runtimes in a clean, Adwaita-based GTK interface.

### Features

* **One-click cleaning** for APT, Flatpak, and thumbnail caches
* **Smart cleanup** options that detect removable safely
* **Live disk usage preview** before cleanup
* **Adwaita-Dark inspired UI** with a green accent
* **Lightweight** built with GTK4 + Libadwaita, under 1 MB
* **Python-based** and easy to extend via modules

---

### Install

The recommended way to install Spruce is throught Flathub:<br>
<br><a href="https://flathub.org/en/apps/io.github.shonubot.Spruce">
    <img width="140" height="60" alt="Spruce Flathub Page" src="https://github.com/user-attachments/assets/48f45b22-c65a-4b3b-b0bc-da0a51293071" />
</a>
```bash
flatpak install flathub io.github.shonubot.Spruce
```
Launch **Spruce** from your app menu

You’ll see a minimal window with clear sections:

* **Cache Cleaner** — remove APT, Flatpak, and thumbnail junk
* **System Logs** — clear systemd journal files
* **Temp Files** — purge /tmp and user-temp safely
* **Preview Changes** — estimate disk space before cleaning


### Contributing

We welcome contributions and improvements!

1. Fork this repo
2. Create a new branch (`feature/your-feature`)
3. Commit your changes
4. Submit a Pull Request
---

### License

Spruce is licensed under the **GNU GPL v3.0**.
You’re free to use, modify, and redistribute it under the same terms.

---

### Screenshots

![Spruce Main Window in light theme](screenshots/Light/Spruce-main.png)
![Spruce Sweep Window in light theme](screenshots/Light/Spruce-sweep.png)
![Spruce Main Window in dark theme](screenshots/Dark/Spruce-main.png)
![Spruce Sweep Window in dark theme](screenshots/Dark/Spruce-sweep.png)

---

# Kiosk Studio

<img src="./io.github.joshuadwight.KioskStudio.svg" alt="Kiosk Studio icon" width="96">

A GTK administration tool for setting up a dedicated kiosk account and approved-app launcher. Choose which apps are available, decide what opens automatically, and deploy the setup from the desktop.

**Version 1.1.0 · Joshua Dwight**

## Install on Ubuntu

Kiosk Studio targets Ubuntu and is designed to work across desktop flavors through the Cage runtime. Linux Mint compatibility and live autologin after reboot still need on-machine verification.

Run these commands from the directory containing this README.

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pkexec
bash install.sh
```

Open **Kiosk Studio** from the application menu. Run the editor as your regular user. Actions such as creating accounts, installing Cage, and deploying the kiosk request administrator authorization.

## Configure a kiosk

1. In **Accounts**, create a dedicated standard account or choose an existing one. For a new account, you can select an additional group. You can also create groups and add existing users to groups. The new account is selected automatically. Use the refresh button next to the account picker to load changes made outside Kiosk Studio.
2. In **Approved Apps**, select the apps to show in the kiosk launcher. Kiosk Studio detects installed graphical apps and can scan additional folders for desktop entries and executable files. Make sure the kiosk account can access the selected apps.
3. In **Autostart**, enable **Launch at startup** for each app that should open when the kiosk session starts. Enter arguments supported by that app. For a browser, expand **Browser URLs** and enter one full HTTP or HTTPS URL per line. The browser receives those URLs after the launch arguments.
4. Enable **Start kiosk automatically at boot**, install the Cage runtime from the **Deployment** tab if needed, and choose **Deploy Current Setup…**. Review the selected account, authorize the change, and reboot when ready.

The deployment changes the machine’s default boot target. On the next reboot, Cage starts under the selected kiosk account in place of the normal graphical sign-in. Keep a separate administrator account for recovery. Cage shows one app at a time, so check which window is in front if you autostart several apps.

## Save, test, and reuse a setup

| File menu option | Use it to |
| --- | --- |
| **Back Up Settings…** | Save the account, approved apps, scan folders, and startup choices to a JSON file. |
| **Restore Settings…** | Load a JSON backup or an existing kiosk session file into the editor. |
| **Create Kiosk Session…** | Write a Python session file for a manual test or for **Deployment → Deploy Session File…**. |

Creating a session file or restoring editor settings leaves the current boot configuration in place. To test a session manually, run the following as the selected kiosk account with the Cage runtime installed.

```bash
dbus-run-session -- cage -- /usr/bin/python3 /path/to/kiosk-user.py
```

Run from a local text console for a dedicated session, or from a graphical desktop for a nested preview. If the machine offers GNOME Kiosk Script Session, its per-user startup script can also run the session file. Automatic boot deployment from Kiosk Studio uses Cage.

## Restore normal boot

Choose **Deployment → Restore Normal Boot…**, authorize the change, and reboot. If the kiosk cannot start, boot once with `systemd.unit=multi-user.target` from the bootloader, sign in as an administrator, and run the removal command below. Adjust the path if you installed the app under a custom data directory.

```bash
sudo /usr/bin/python3 "${XDG_DATA_HOME:-$HOME/.local/share}/kiosk-studio/deploy.py" --remove
```

## Kiosk boundaries

The approved-app list controls the apps shown in the launcher. Approved apps retain their own capabilities, including file access, external links, and launching other programs where their settings allow it. Configure and test the apps under the kiosk account before deploying to a public machine.

## Launchers and help

**File → Install in Application Menu** creates or refreshes the administrator app’s menu entry. **File → Install Desktop Shortcut** creates or replaces its desktop launcher.

Open **Help → User Guide** for a full walkthrough or **Help → About** for version information. [User guide](./USER-GUIDE.txt) · [Changelog](./changelog.md)

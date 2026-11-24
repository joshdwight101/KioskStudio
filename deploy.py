#!/usr/bin/python3
"""Install or remove a Kiosk Studio boot profile without restarting the system."""
import argparse
import json
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from session import startup_arguments, validate_startup

UNITS = Path('/etc/systemd/system')
STATE = Path('/var/lib/kiosk-studio')
TARGET = 'kiosk-studio.target'
SERVICE = 'kiosk-studio.service'
PREFIX = '# Kiosk Studio profile: '
BIN = Path(__file__).resolve().parent


def systemctl(*args):
    return subprocess.check_output(['systemctl', *args], text=True).strip()


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.chmod(0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def load_profile(path):
    with path.open(encoding='utf-8') as stream:
        stream.readline()
        line = stream.readline()
    if not line.startswith(PREFIX):
        raise ValueError('Select a session exported by Kiosk Studio.')
    profile = json.loads(line[len(PREFIX):])
    uid = profile['uid']
    if type(uid) is not int or not 1000 <= uid < 65534:
        raise ValueError('Select a dedicated standard local account.')
    user = pwd.getpwuid(uid)
    if user.pw_name != profile['user']:
        raise ValueError('The exported account does not match this computer.')
    groups = subprocess.check_output(['id', '-Gn', '--', user.pw_name], text=True).split()
    if set(groups) & {'sudo', 'admin', 'wheel', 'root', 'docker', 'lxd', 'incus', 'disk', 'libvirt'}:
        raise ValueError('The kiosk account has administrator or system-level group access.')
    if profile['autologin'] is not True:
        raise ValueError('Enable automatic startup before exporting a boot profile.')
    if not isinstance(profile['apps'], list) or not profile['apps']:
        raise ValueError('The profile needs at least one app.')
    startup = validate_startup(profile.get('startup', {}))
    for app in profile['apps']:
        if (app['kind'] not in {'desktop', 'executable'} or not isinstance(app['name'], str)
                or not Path(app['path']).is_absolute() or not Path(app['path']).is_file()):
            raise ValueError('The profile contains an unavailable application.')
        options = startup.get(app['path'])
        if options and options['enabled']:
            startup_arguments(options)
    return profile


def install(profile):
    for dependency in ('/usr/bin/cage', '/usr/bin/chvt', '/usr/bin/dbus-run-session'):
        if not os.access(dependency, os.X_OK):
            raise ValueError(f'Missing {dependency}. Install Cage and the session dependencies first.')
    previous = STATE / 'previous-target'
    current = systemctl('get-default')
    if current == TARGET and not previous.is_file():
        raise ValueError('Cannot identify the previous boot target.')
    runtime = (BIN / 'session.py').read_text(encoding='utf-8')
    content = f"{runtime}\n\nif __name__ == '__main__':\n    raise SystemExit(run_session({profile!r}))\n"
    compile(content, 'session.py', 'exec')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not previous.exists():
        atomic_write(previous, current + '\n')
    atomic_write(STATE / 'session.py', content)
    atomic_write(UNITS / SERVICE, f'''[Unit]
Description=Kiosk Studio approved application session
After=systemd-user-sessions.service
Conflicts=display-manager.service
StartLimitIntervalSec=30
StartLimitBurst=4

[Service]
User={profile['uid']}
PAMName=login
Environment=XDG_SESSION_TYPE=wayland
StandardInput=tty-force
StandardOutput=journal
StandardError=journal
TTYPath=/dev/tty7
TTYReset=yes
TTYVHangup=yes
ExecStartPre=+/usr/bin/chvt 7
ExecStart=/usr/bin/dbus-run-session -- /usr/bin/cage -- /usr/bin/python3 /var/lib/kiosk-studio/session.py
Restart=always
RestartSec=3
''')
    atomic_write(UNITS / TARGET, f'''[Unit]
Description=Kiosk Studio boot
Requires=multi-user.target
Wants={SERVICE}
After=multi-user.target
AllowIsolate=yes
''')
    systemctl('daemon-reload')
    systemctl('set-default', TARGET)
    print(f"Next boot will start the kiosk as {profile['user']}. The current session is still running.")


def remove():
    previous = STATE / 'previous-target'
    if not previous.is_file():
        raise ValueError('No installed Kiosk Studio boot profile was found.')
    if systemctl('get-default') == TARGET:
        systemctl('set-default', previous.read_text(encoding='utf-8').strip())
    for path in (UNITS / SERVICE, UNITS / TARGET, STATE / 'session.py', previous):
        path.unlink(missing_ok=True)
    systemctl('daemon-reload')
    print('Kiosk boot removed. Restart to return to the previous boot mode.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', type=Path, metavar='EXPORTED_SESSION')
    mode.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'Run deployment with administrator privileges using sudo.\n')
    try:
        if args.remove:
            remove()
        else:
            install(load_profile(args.apply))
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()

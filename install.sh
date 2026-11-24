#!/usr/bin/env bash
set -euo pipefail
source_dir="$(dirname "$(realpath "$0")")"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
app_dir="$data_dir/kiosk-studio"
app_id=io.github.joshuadwight.KioskStudio
install -d "$app_dir" "$data_dir/applications"
install -m 755 "$source_dir/kiosk_studio.py" "$source_dir/deploy.py" "$app_dir/"
install -m 644 "$source_dir/session.py" "$source_dir/README.md" "$source_dir/USER-GUIDE.txt" "$source_dir/changelog.md" "$app_dir/"
install -Dm644 "$source_dir/$app_id.svg" "$data_dir/icons/hicolor/scalable/apps/$app_id.svg"
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -ft "$data_dir/icons/hicolor"
fi
cat > "$data_dir/applications/$app_id.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Kiosk Studio
Comment=Configure approved applications for a kiosk session
Exec=/usr/bin/python3 "$app_dir/kiosk_studio.py"
Icon=$app_id
Terminal=false
Categories=System;
EOF
printf 'Kiosk Studio installed. Open it from the application menu.\n'

# EmergencyBox

An offline information box for a family: a Wi-Fi hotspot with a library (Wikipedia and more),
books, movies, maps, games, printing and file storage, all reachable from any phone or laptop
that joins its Wi-Fi. No internet needed once it's installed.

## What you need

- A Raspberry Pi 4 or 5 (4 GB RAM or more) with an SSD, or a 64-bit PC or laptop
- Raspberry Pi OS Lite 64-bit (Debian 13), or Debian/Ubuntu on a PC
- Internet over Ethernet during the install (the Wi-Fi becomes the box's hotspot)

## Install

```bash
sudo apt update && sudo apt install -y git        # Raspberry Pi OS Lite comes without git
git clone https://github.com/Sentinel6Systems/emergencybox-installer.git emergencybox && cd emergencybox
cp box.conf.default box.conf && nano box.conf   # optional: name, Wi-Fi, map area, time zone
sudo ./install.sh
sudo reboot                                       # if the installer says a restart is needed
```

Everything installs on the system disk. Each part checks itself at the end; if one stops, it
says what to fix and which command to run again. Running `sudo ./install.sh` again is always
safe and changes nothing that is already right.

When it's done, join the box's Wi-Fi (default `EmergencyBox` / `stayready`) and open
`http://box.lan`. The box's own address is shown next to it (each box picks its own
`10.x.x.1` address on the first install).

## Accounts

The same two logins everywhere. They are generic on purpose; change them on the admin page.

| Login | Default password | For |
|---|---|---|
| `admin` | `keepready` | Admin page, Jellyfin and Calibre-Web administration |
| `user` | `stayready` | Everyone else (Jellyfin) |

## Adding content (last step)

The box starts empty: every card says "Nothing added yet", except Maps, which starts with a
world map down to about county level (zoom 0-8, downloaded during the install). Add what you
want, either on the admin page (`http://box.lan/box/admin/`) or by copying files as `admin`
over SSH. No sudo is needed to copy into these folders:

| What | Where |
|---|---|
| Library (`.zim` files from library.kiwix.org) | `/srv/box/content/zims/` (picked up by itself) |
| Movies and shows | One folder per movie or show: `/srv/box/content/media/<library>/<Title>/` (see below) |
| Game ROMs | `/srv/box/content/games/roms/<system>/` |
| Books | Sign in to Calibre-Web (`/books/`) as admin and use Upload |
| Maps for your area | Build them on the Map Builder (`http://box.lan/mapadmin/`; tick Include World Overview), or copy `.mbtiles` files into `/opt/fieldmap-server/data/` and run `sudo ./install.sh maps` |

Keep each movie in its own folder, with its subtitles and poster named so Jellyfin finds them:
`movies/Planet Earth (2006)/Planet Earth (2006).mp4`, `Planet Earth (2006).en.srt` and `poster.jpg`.
The admin page does this naming for you. A show keeps its episodes' names in the show's folder
(`shows/<Show>/Show.S01E01.mp4`). New files are picked up within a minute, except the first
movie in an empty library: Jellyfin only watches a library after a scan has found something in
it. The admin page starts that scan by itself; after copying over SSH, start it from Jellyfin's
dashboard (Libraries, Scan All Libraries).

For example, from another computer:

```bash
rsync -av --progress my-zims/ admin@box.lan:/srv/box/content/zims/
```

## Parts

`sudo ./install.sh --list` shows them. One part at a time: `sudo ./install.sh library`.

## Ports

| Port | Service |
|---|---|
| 80 | nginx (every page) |
| 631 | Printing (CUPS) |
| 8083, 8096, 8181-8185 | Calibre-Web, Jellyfin, box services (127.0.0.1 only) |
| 8080, 8091-8093, 17777 | fieldmap-server (127.0.0.1 only) |

## License

GPL-3.0 (`LICENSE`). Bundled third-party files and their licenses: `THIRD-PARTY.md`.

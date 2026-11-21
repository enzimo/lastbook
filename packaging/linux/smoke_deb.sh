#!/bin/bash
# Smoke test of a built .deb in a clean copy of the release it was built for (a fresh ubuntu:24.04, ubuntu:26.04 or debian:13 container, as root):
#   docker run --rm -v <folder with the .deb and LastbookData>:/t ubuntu:24.04 bash /t/smoke_deb.sh /t/Lastbook_<v>_amd64.deb /t/LastbookData
# Installs the .deb with apt (so Depends are resolved from the release's archive), starts it under Xvfb with a session bus as an ordinary
# user, once on a library and once with none (setup), checks the window, the log and one copy only, takes screenshots into /t/shots,
# then removes the package and checks the person's folders are still there.
set -uo pipefail
DEB="$1"; LIB="$2"; OUTD="$(dirname "$DEB")/shots"
mkdir -p "$OUTD" && chmod 777 "$OUTD"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq "$DEB" xvfb xauth dbus-x11 x11-apps imagemagick xdotool procps iproute2 >/tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; exit 1; }
echo "== installed: $(dpkg-query -W -f '${Package} ${Version}\n' lastbook)"
apt-get install -y -qq gstreamer1.0-tools >/dev/null 2>&1   # gst-inspect, for this check only
gst-inspect-1.0 avdec_h264 >/dev/null 2>&1 && echo "== H.264 decoder present (gstreamer1.0-libav)" || echo "== NO H.264 decoder"
gst-inspect-1.0 matroskademux >/dev/null 2>&1 && echo "== WebM/Matroska demuxer present" || echo "== NO matroskademux"
useradd -m tester
cp -a "$LIB" /home/tester/LastbookData && chown -R tester: /home/tester/LastbookData
# a real H.264/AAC MP4 in place of the fixture's videos/a.mp4 (patterned bytes), so the player has something to decode
apt-get install -y -qq ffmpeg openbox >/dev/null 2>&1
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=640x360:rate=25 -f lavfi -i sine=frequency=440 -t 20 -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart /home/tester/LastbookData/library-data/videos/a.mp4 && chown tester: /home/tester/LastbookData/library-data/videos/a.mp4

run_as() { su tester -s /bin/bash -c "$1"; }
shot() { run_as "DISPLAY=:99 import -window root '$OUTD/$1.png'" 2>/dev/null; echo "== screenshot $1.png"; }

run_as "Xvfb :99 -screen 0 1360x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &"
sleep 2
run_as "DISPLAY=:99 openbox >/tmp/wm.log 2>&1 &"
sleep 1
run_as "cd ~ && DISPLAY=:99 dbus-daemon --session --fork --address=unix:path=/tmp/tester-bus --print-address >/dev/null"
ENV="DISPLAY=:99 DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/tester-bus"

echo "== 1. with a library"
run_as "$ENV lastbook --data /home/tester/LastbookData >/tmp/run1.out 2>&1 &"
sleep 25
pgrep -u tester -f /opt/lastbook/lastbook.py >/dev/null && echo "running: yes" || { echo "running: NO"; cat /tmp/run1.out; }
run_as "$ENV xdotool search --name '^Lastbook$'" >/dev/null && echo "window: yes" || echo "window: NO"
shot library-home
run_as "$ENV xdotool search --name '^Lastbook$' windowactivate --sync key ctrl+2" 2>/dev/null; sleep 6; shot library-map
run_as "$ENV xdotool search --name '^Lastbook$' windowactivate --sync key ctrl+1" 2>/dev/null; sleep 6; shot library-videos
echo "-- a video: open its card, then play"
W="$ENV xdotool search --name '^Lastbook$'"
run_as "$W windowmove 0 0 windowsize 1280 860" 2>/dev/null; sleep 2
run_as "$W windowactivate --sync key ctrl+1" 2>/dev/null; sleep 3; shot videos-page
run_as "$ENV xdotool mousemove 428 611 click 1"; sleep 5; shot video-opened
run_as "$ENV xdotool mousemove 600 330 click 1"; sleep 4; shot video-playing
grep -c "couldn" /dev/null >/dev/null
echo "-- second copy (must leave at once, the first comes forward)"
start=$(date +%s); run_as "$ENV timeout 30 lastbook --data /home/tester/LastbookData"; echo "second copy exit=$? after $(( $(date +%s) - start )) s"
echo "-- listening sockets of the app (must be none)"
for p in $(pgrep -u tester -f 'lastbook|WebKit'); do ls -l /proc/$p/fd 2>/dev/null; done | grep -c socket | sed 's/^/sockets (unix, to X and D-Bus) /'
ss -ltnup 2>/dev/null | grep -v '^Netid' | sed 's/^/LISTEN: /' || true
echo "-- log"
tail -25 /home/tester/LastbookData/derived/logs/lastbook.log
echo "-- Quit (Ctrl+Q): the app and its WebKit processes must end"
run_as "export $ENV; W=\$(xdotool search --name '^Lastbook\$' | head -1); xdotool windowactivate --sync \$W key ctrl+q" 2>/dev/null; sleep 6
ps -u tester -o stat=,args= | grep -v '^Z' | grep -qE '/opt/lastbook/lastbook.py|WebKit' && { echo "quit: STILL RUNNING"; ps -u tester -o pid,stat,args | grep -E 'lastbook|WebKit' | cut -c1-120; tail -2 /home/tester/LastbookData/derived/logs/lastbook.log; pkill -u tester -f /opt/lastbook/lastbook.py; sleep 2; } || echo "quit: ended"

echo "== 2. no library: setup"
run_as "$ENV lastbook >/tmp/run2.out 2>&1 &"
sleep 20
pgrep -u tester -f /opt/lastbook/lastbook.py >/dev/null && echo "running: yes" || { echo "running: NO"; cat /tmp/run2.out; }
shot setup
tail -15 /home/tester/.local/share/Lastbook/Setup/derived/logs/lastbook.log 2>/dev/null
pkill -u tester -f /opt/lastbook/lastbook.py; sleep 2

echo "== 3. remove"
apt-get remove -y -qq lastbook >/dev/null 2>&1
dpkg-query -W lastbook 2>/dev/null | grep -q . && dpkg -l lastbook | tail -1
[ -e /opt/lastbook ] && { echo "left behind:"; find /opt/lastbook | head; } || echo "/opt/lastbook gone"
[ -f /home/tester/LastbookData/library-data/catalog.json ] && echo "library kept" || echo "LIBRARY GONE"
ls -d /home/tester/.config/Lastbook /home/tester/.local/share/Lastbook 2>/dev/null | sed 's/^/kept: /'
chmod -R a+rX "$OUTD"

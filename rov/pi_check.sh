#!/bin/bash
# Pi-side check for video lag spikes / stutter. Run it WHILE the camera is streaming:
#   ./pi_check.sh            # 30 s
#   ./pi_check.sh 60         # 60 s
#   ./pi_check.sh 60 wlan0   # pick the network interface (default: the one that reaches ROV_TOPSIDE_IP)
#
# Looks at what the laptop-side tool (topside/video_check.py) can't see: Wi-Fi signal, retries and
# power saving, packets the Pi dropped before sending, CPU, temperature, USB disconnects, camera settings.

SECS="${1:-30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/video.env" ] && { set -a; . "$SCRIPT_DIR/video.env"; set +a; }
TOPSIDE="${ROV_TOPSIDE_IP:-192.168.1.69}"
IF="${2:-$(ip route get "$TOPSIDE" 2>/dev/null | grep -o 'dev [^ ]*' | cut -d' ' -f2)}"
[ -z "$IF" ] && { echo "Can't tell which interface reaches $TOPSIDE; pass it: ./pi_check.sh $SECS wlan0"; exit 1; }
WIFI=false; [ -d "/sys/class/net/$IF/wireless" ] && WIFI=true
HAVE_IW=false; command -v iw >/dev/null 2>&1 && HAVE_IW=true
HINTS=()

udp() { awk '/^Udp:/{ if (!h) { for (i=1;i<=NF;i++) n[i]=$i; h=1 } else { for (i=1;i<=NF;i++) if (n[i]==k) print $i } }' k="$1" /proc/net/snmp; }
stat_of() { cat "/sys/class/net/$IF/statistics/$1" 2>/dev/null || echo 0; }
station() { iw dev "$IF" station dump 2>/dev/null | awk -v k="$1" -F':' '$1 ~ k {gsub(/[^0-9]/,"",$2); s+=$2} END {print s+0}'; }
cpu_jiffies() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat; }
disconnects() { local out; out=$(sudo -n dmesg 2>/dev/null) || { echo "?"; return; }; grep -c 'USB disconnect' <<<"$out"; }
temp_c() { awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null; }

echo "Pi check: $SECS s on $IF (to $TOPSIDE). Keep the camera streaming."
[ "$(pgrep -fc 'gst-launch-1.0 v4l2src')" -eq 0 ] && echo "WARNING: no camera stream is running right now (start ./run.sh or rov-video first)."
sudo -v 2>/dev/null || true

# ---- before
tx0=$(stat_of tx_bytes); txp0=$(stat_of tx_packets); txd0=$(stat_of tx_dropped); txe0=$(stat_of tx_errors)
snd0=$(udp SndbufErrors); usb0=$(disconnects)
read -r cpuT0 cpuI0 <<<"$(cpu_jiffies)"
if $WIFI && $HAVE_IW; then rt0=$(station "tx retries"); tf0=$(station "tx failed"); tp0=$(station "tx packets"); fi
t_start=$(temp_c)
freq_min=999999
for ((i=0; i<SECS; i++)); do
    f=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)
    (( f > 0 && f < freq_min )) && freq_min=$f
    sleep 1
done
# ---- after
tx1=$(stat_of tx_bytes); txp1=$(stat_of tx_packets); txd1=$(stat_of tx_dropped); txe1=$(stat_of tx_errors)
snd1=$(udp SndbufErrors); usb1=$(disconnects)
read -r cpuT1 cpuI1 <<<"$(cpu_jiffies)"
if $WIFI && $HAVE_IW; then rt1=$(station "tx retries"); tf1=$(station "tx failed"); tp1=$(station "tx packets"); fi
t_end=$(temp_c)

echo
echo "== Network ($IF)"
echo "  sent: $(( (tx1-tx0)*8/SECS/1000 )) kbit/s average, $(( txp1-txp0 )) packets"
echo "  interface drops/errors: $(( txd1-txd0 )) / $(( txe1-txe0 ))"
echo "  times the Pi's send queue was full (UDP SndbufErrors): $(( snd1-snd0 ))"
# Not necessarily loss: GStreamer can retry these. Real loss shows up in video_check.py on the laptop.
(( snd1-snd0 > 1000 )) && HINTS+=("The Pi's send queue filled $(( snd1-snd0 )) times: the stream is near what this link can carry. Not loss by itself; check video_check.py's packet loss on the laptop. If that's high too, use the tether, 720p, or --jpeg-quality.")
(( txd1-txd0 > 0 )) && HINTS+=("The network interface dropped $(( txd1-txd0 )) packets: the link is saturated.")

if $WIFI; then
    echo "== Wi-Fi"
    if $HAVE_IW; then
        iw dev "$IF" link | grep -E 'SSID|freq|signal|tx bitrate|rx bitrate' | sed 's/^\s*/  /'
        ps=$(iw dev "$IF" get power_save 2>/dev/null | awk '{print $NF}')
        echo "  power saving: ${ps:-unknown}"
        [ "$ps" = "on" ] && HINTS+=("Wi-Fi power saving is ON on the Pi; it causes periodic lag spikes. Turn it off now: sudo iw dev $IF set power_save off  (permanently: rerun the install/setup script). On the laptop: sudo iw dev wlo1 set power_save off")
        sig=$(iw dev "$IF" link | awk '/signal/{print $2}')
        [ -n "$sig" ] && (( sig < -67 )) && HINTS+=("Weak Wi-Fi signal ($sig dBm). Below about -67 dBm, retries and delay spikes climb; move closer to the access point or use the tether.")
        freq=$(iw dev "$IF" link | awk '/freq/{print int($2)}')
        [ -n "$freq" ] && (( freq < 3000 )) && HINTS+=("Connected on 2.4 GHz ($freq MHz). 2.4 GHz is slower and more crowded; 1080p MJPEG needs 5 GHz or the tether.")
        dp=$(( tp1-tp0 )); dr=$(( rt1-rt0 )); df=$(( tf1-tf0 ))
        if (( dp > 0 )); then
            echo "  retries: $dr ($(( 100*dr/dp ))% of $dp packets), failed: $df"
            (( 100*dr/dp >= 10 )) && HINTS+=("Wi-Fi retries are high ($(( 100*dr/dp ))%): interference or a busy channel. Each retry burst is a delay spike.")
            (( df > 0 )) && HINTS+=("$df Wi-Fi packets failed outright (gave up after retries): those are lost frames.")
        fi
    else
        awk -v i="$IF" '$1 ~ i {print "  link quality:", $3, " signal:", $4}' /proc/net/wireless
        echo "  (install iw for power-save, retry and bitrate details: sudo dnf install -y iw)"
    fi
fi

echo "== Pi"
busy=$(( 100 * ( (cpuT1-cpuT0) - (cpuI1-cpuI0) ) / ( (cpuT1-cpuT0) > 0 ? (cpuT1-cpuT0) : 1 ) ))
echo "  CPU busy (all cores): ${busy}%   load average: $(cut -d' ' -f1-3 /proc/loadavg)"
ps -C gst-launch-1.0 -o pid=,%cpu=,args= 2>/dev/null | while read -r pid cpu args; do
    caps=$(grep -oE '(image/jpeg|video/x-h264),width=[0-9]+,height=[0-9]+,framerate=[0-9/]+' <<<"$args")
    dest=$(grep -oE 'host=[^ ]+ port=[0-9]+' <<<"$args")
    echo "  stream: pid $pid, ${cpu}% CPU, ${caps:-?} -> ${dest:-?}"
done
(( busy > 80 )) && HINTS+=("The Pi's CPU is ${busy}% busy; frames can be sent late. Use MJPEG or camera-h264 (no encoding on the Pi), or lower the resolution.")
[ -n "$t_end" ] && echo "  temperature: ${t_start} -> ${t_end} C"
(( freq_min < 999999 )) && echo "  lowest CPU clock seen: $(( freq_min/1000 )) MHz"
[ -n "$t_end" ] && awk -v t="$t_end" 'BEGIN { exit !(t >= 80) }' && HINTS+=("The Pi is at ${t_end} C; at about 85 C it throttles. Add cooling.")
if [ "$usb0" != "?" ] && [ "$usb1" != "?" ]; then
    echo "  USB disconnects during the test: $(( usb1-usb0 ))"
    (( usb1-usb0 > 0 )) && HINTS+=("The camera disconnected from USB during the test: check the port, cable and power (see sudo dmesg | tail).")
else
    echo "  USB disconnects: (run once with sudo access to check)"
fi

echo "== Camera"
node=$(ls /dev/v4l/by-path/*usb*-video-index0 2>/dev/null | head -1)
if [ -n "$node" ] && command -v v4l2-ctl >/dev/null; then
    ctrls=$(v4l2-ctl -d "$node" --get-ctrl=exposure_dynamic_framerate,power_line_frequency,auto_exposure 2>/dev/null)
    echo "$ctrls" | sed 's/^/  /'
    echo "$ctrls" | grep -q 'exposure_dynamic_framerate: 1' && HINTS+=("exposure_dynamic_framerate is on: in dim light the camera lowers its frame rate (uneven frames). Turn it off: v4l2-ctl -d $node --set-ctrl=exposure_dynamic_framerate=0")
    echo "$ctrls" | grep -q 'power_line_frequency: 1' && HINTS+=("power_line_frequency is 50 Hz; US mains is 60 Hz, which can flicker under pool lights: v4l2-ctl -d $node --set-ctrl=power_line_frequency=2")
fi

echo
echo "== What to look at"
if (( ${#HINTS[@]} == 0 )); then
    echo "  Nothing obvious on the Pi side. Compare with the laptop: python3 video_check.py capture --seconds 60"
else
    for h in "${HINTS[@]}"; do echo "  - $h"; done
fi

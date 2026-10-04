#!/bin/bash
# Watchdog: keep the two hybrid downloads moving.
# Restarts an aria2 (with -c resume) when its latest DL sample is too low
# or when its log goes stale. Never restarts within 120s of a restart.

declare -A LAST_RESTART

INT8_URL="https://hf-mirror.com/smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models/resolve/main/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"
W6A8_URL="https://hf-mirror.com/binglingzhimeng/minimax_h3_hybrid_fl2va_ref2va_b25-49_w6a8/resolve/main/minimax_h3_hybrid_fl2va_ref2va_b25-49_w6a8.safetensors"

restart_dl() {
    local name="$1" url="$2" out="$3"
    pkill -f "aria2c.*$out" 2>/dev/null
    sleep 2
    cd /root/ComfyUI/models || return
    setsid aria2c -x16 -s16 -k4M -c --file-allocation=none \
        --summary-interval=30 --console-log-level=warn --max-tries=0 --retry-wait=3 \
        -d diffusion_models -o "$out" "$url" >> "/root/h3_dl_$name.log" 2>&1 &
    LAST_RESTART[$name]=$(date +%s)
    echo "$(date '+%H:%M:%S') RESTART $name" >> /root/h3_watchdog.log
}

to_kib() {
    # "740KiB" | "1.8MiB" -> integer KiB
    local v="$1"
    case "$v" in
        *KiB) echo "${v%KiB}" | cut -d. -f1 ;;
        *MiB) echo $(( $(echo "${v%MiB}" | cut -d. -f1) * 1024 )) ;;
        *) echo 0 ;;
    esac
}

while true; do
    for spec in "int8|$INT8_URL|minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors" \
                "w6a8|$W6A8_URL|minimax_h3_hybrid_fl2va_ref2va_b25-49_w6a8.safetensors"; do
        IFS='|' read -r name url out <<< "$spec"
        ctl="/root/ComfyUI/models/diffusion_models/$out.aria2"
        [ -f "$ctl" ] || { echo "$(date '+%H:%M:%S') DONE $name" >> /root/h3_watchdog.log; continue; }

        last=${LAST_RESTART[$name]:-0}
        now=$(date +%s)
        [ $((now - last)) -lt 120 ] && continue

        pid=$(pgrep -f "aria2c.*$out" | head -1)
        if [ -z "$pid" ]; then
            restart_dl "$name" "$url" "$out"
            continue
        fi

        log="/root/h3_dl_$name.log"
        age=$(( now - $(stat -c %Y "$log") ))
        if [ "$age" -gt 180 ]; then
            echo "$(date '+%H:%M:%S') STALE log ($age s) for $name" >> /root/h3_watchdog.log
            restart_dl "$name" "$url" "$out"
            continue
        fi

        dl=$(grep -oE "DL:[0-9.]+[KM]iB" "$log" | tail -1 | sed 's/DL://')
        kib=$(to_kib "$dl")
        if [ "$kib" -lt 1500 ]; then
            echo "$(date '+%H:%M:%S') SLOW $name ($dl)" >> /root/h3_watchdog.log
            restart_dl "$name" "$url" "$out"
        fi
    done
    sleep 60
done

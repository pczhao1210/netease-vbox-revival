#!/bin/sh

export LC_ALL=C

player_from_text() {
	PLAYER=$(printf '%s\n' "$1" | sed -n 's/.*curPlayer(\([0-9]*\)).*/\1/p' | tail -1)
	if [ -z "$PLAYER" ]; then
		PLAYER=$(printf '%s\n' "$1" | sed -n 's/.*"playerId"[ ]*:[ ]*\([0-9]*\).*/\1/p' | tail -1)
	fi
	case "$PLAYER" in 1|2) printf '%s\n' "$PLAYER" ;; *) printf '1\n' ;; esac
}

current_player() {
	CONTROL_LOG=$(ls -t /tmp/netease_control_center_*.log 2>/dev/null | head -1)
	TEXT=$(tail -c 100000 "$CONTROL_LOG" 2>/dev/null | grep -E 'not the curPlayer|cmdId:0x601')
	player_from_text "$TEXT"
}

player_line() {
	LATEST=$(ls -t /tmp/ihwplayer_*.log 2>/dev/null | head -1)
	[ -n "$LATEST" ] || return 0
	tail -c 100000 "$LATEST" 2>/dev/null | grep "playerCallbackHandler(755).*playerId:$PLAYER_ID st:0x" | tail -1
}

validate_wire() {
	WIRE=$1
	case "$WIRE" in *'
'*) return 1 ;; esac
	TAB=$(printf '\t')
	IFS="$TAB" read -r VERSION COMMAND_ID GENERATION TTL ACTION ARGUMENT EXTRA <<EOF
$WIRE
EOF
	[ "$VERSION" = v2 ] && [ -z "$EXTRA" ] || return 1
	[ "${#COMMAND_ID}" = 32 ] || return 1
	case "$COMMAND_ID" in *[!0-9a-f]*) return 1 ;; esac
	case "$GENERATION:$TTL" in *[!0-9:]*|:*) return 1 ;; esac
	[ "${#GENERATION}" -le 9 ] && [ "${#TTL}" -le 3 ] || return 1
	[ "$TTL" -ge 1 ] && [ "$TTL" -le 120 ] || return 1
	case "$ACTION" in
	play)
		[ "${#ARGUMENT}" -le 4096 ] || return 1
		printf '%s\n' "$ARGUMENT" | grep -Eq '^https?://[A-Za-z0-9._~:/?&=,%+@!()-]+$' || return 1
		;;
	stop|quiet_on) case "$ARGUMENT" in day|night|-) ;; *) return 1 ;; esac ;;
	status|quiet_off) [ "$ARGUMENT" = - ] || return 1 ;;
	quiet_schedule)
		case "$ARGUMENT" in off) ;; *)
			printf '%s\n' "$ARGUMENT" | grep -Eq '^([0-9]|1[0-9]|2[0-3]),([0-9]|1[0-9]|2[0-3])$' || return 1 ;;
		esac
		;;
	*) return 1 ;;
	esac
}

report() {
	curl -fsS --connect-timeout 2 -m 5 -X POST \
		-H "Authorization: Bearer $TOKEN" \
		--data "id=$COMMAND_ID&generation=$GENERATION&result=$1&boot_id=$BOOT" \
		"$SERVER/device/result" >/dev/null 2>&1
}

observe() {
	[ -n "$PLAY_ID" ] || return 0
	LINE=$(player_line)
	[ "$LINE" = "$LAST_LINE" ] && return 0
	STATE=$(printf '%s\n' "$LINE" | sed -n 's/.*playerId:[12] st:\(0x[0-9a-f]*\).*/\1/p')
	COMMAND_ID=$PLAY_ID
	GENERATION=$PLAY_GENERATION
	# Keep LAST_LINE unchanged until delivery succeeds so a lost report is retried.
	# 0x4 means the native controller paused us: phone cast, button, or its pre-end skip.
	case "$STATE" in
	0x3) report playing || return 0; SEEN_PLAY=1 ;;
	0x0) if [ "$SEEN_PLAY" = 1 ]; then report completed || return 0; PLAY_ID=; SEEN_PLAY=0; fi ;;
	0x4) if [ "$SEEN_PLAY" = 1 ]; then report stopped || return 0; PLAY_ID=; SEEN_PLAY=0; fi ;;
	esac
	LAST_LINE=$LINE
}

acquire_lock() {
	LOCK="$RUN/agent.pid"
	for LOCK_ATTEMPT in 1 2 3; do
		ln -s "$$" "$LOCK" 2>/dev/null && return 0
		OWNER=$(readlink "$LOCK") || return 1
		case "$OWNER" in
		''|*[!0-9]*) ;;
		*)
			if { tr '\0' '\n' < "/proc/$OWNER/cmdline"; } 2>/dev/null | grep -Fx "$AGENT_PATH" >/dev/null; then
				return 1
			fi
			;;
		esac
		[ "$(readlink "$LOCK")" = "$OWNER" ] || continue
		rm -f "$LOCK" || return 1
	done
	return 1
}

release_lock() {
	[ "$(readlink "$LOCK")" = "$$" ] && rm -f "$LOCK"
}

update_quiet_schedule() {
	QUIET_VALUE=$(printf '%s\n' "$ARGUMENT" | tr ',' ' ')
	[ "$(cat "$DIR/quiet.conf" 2>/dev/null)" = "$QUIET_VALUE" ] && return 0
	QUIET_TEMP="$DIR/.quiet.conf.$$"
	if printf '%s\n' "$QUIET_VALUE" > "$QUIET_TEMP" && mv -f "$QUIET_TEMP" "$DIR/quiet.conf"; then
		return 0
	fi
	rm -f "$QUIET_TEMP"
	return 1
}

main() {
	umask 077
	DIR=${SPK_DIR:-/mnt/UDISK/spk}
	[ -f "$DIR/spk_conf.sh" ] || return 1
	. "$DIR/spk_conf.sh"
	TOKEN=$(cat "$DIR/net.token" 2>/dev/null)
	[ "${#TOKEN}" -ge 32 ] || { echo 'Device token is missing or too short' >&2; return 1; }
	SERVER=${SPK_NET_SRV:-$SPK_SRV:$SPK_PORT_NETD}
	case "$SERVER" in http://*|https://*) ;; *) SERVER="http://$SERVER" ;; esac
	RUN=/tmp/spk/v2
	mkdir -p "$RUN"
	AGENT_PATH=$0
	acquire_lock || return 0
	trap 'release_lock' EXIT
	trap 'exit 0' HUP INT TERM
	BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
	LAST_ID=$(cat "$RUN/id" 2>/dev/null)
	LAST_GENERATION=$(cat "$RUN/generation" 2>/dev/null)
	LAST_GENERATION=${LAST_GENERATION:-0}
	PLAY_ID=
	PLAY_GENERATION=0
	PLAYER_ID=1
	LAST_LINE=
	SEEN_PLAY=0
	while true; do
		observe
		STARTED=$(date +%s)
		if ! WIRE=$(curl -fsS --connect-timeout 2 -m 5 -H "Authorization: Bearer $TOKEN" -H "X-Device-Boot-ID: $BOOT" "$SERVER/device/cmd" 2>/dev/null); then
			sleep 1
			continue
		fi
		if [ -z "$WIRE" ]; then
			# The server holds empty polls; only an immediate empty answer needs throttling.
			[ "$(date +%s)" -gt "$STARTED" ] || sleep 1
			continue
		fi
		validate_wire "$WIRE" || { sleep 1; continue; }
		[ "$GENERATION" -ge "$LAST_GENERATION" ] || continue
		if [ "$COMMAND_ID" = "$LAST_ID" ]; then report accepted; continue; fi
		case "$ACTION" in
		play)
			PLAYER_ID=$(current_player)
			LAST_LINE=$(player_line)
			"$DIR/play601.sh" play "$ARGUMENT" "$PLAYER_ID" >/dev/null 2>&1 || { report failed; continue; }
			PLAY_ID=$COMMAND_ID; PLAY_GENERATION=$GENERATION; SEEN_PLAY=0
			;;
		stop)
			PLAYER_ID=$(current_player)
			"$DIR/play601.sh" stop "$PLAYER_ID" >/dev/null 2>&1 || { report failed; continue; }
			PLAY_ID=; SEEN_PLAY=0
			rmdir /tmp/spk/ringing 2>/dev/null
			if [ "$ARGUMENT" = night ]; then amixer -c 0 cset numid=105 off >/dev/null 2>&1; amixer -c 0 cset numid=106 off >/dev/null 2>&1; fi
			;;
		quiet_off)
			mkdir -p /tmp/spk/ringing
			amixer -c 0 cset numid=105 on >/dev/null 2>&1
			amixer -c 0 cset numid=106 on >/dev/null 2>&1
			;;
		quiet_on)
			rmdir /tmp/spk/ringing 2>/dev/null
			if [ "$ARGUMENT" = night ]; then amixer -c 0 cset numid=105 off >/dev/null 2>&1; amixer -c 0 cset numid=106 off >/dev/null 2>&1; fi
			;;
		quiet_schedule)
			update_quiet_schedule || { report failed; continue; }
			;;
		status) ;;
		esac
		LAST_ID=$COMMAND_ID; LAST_GENERATION=$GENERATION
		printf '%s\n' "$LAST_ID" > "$RUN/id"
		printf '%s\n' "$LAST_GENERATION" > "$RUN/generation"
		report accepted
	done
}

if [ "${1:-}" = --validate ]; then
	validate_wire "$2"
elif [ "${1:-}" = --player-id ]; then
	player_from_text "$2"
else
	main
fi
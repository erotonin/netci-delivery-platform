#!/bin/sh
# Stands in for Jenkins in lab/spike/many_volumes_probe.py: the netci-cell chart, cell agent,
# guard, Lease and volume are the real ones, and only Jenkins is replaced -- by this, which
# answers the chart's probes on /login and appends a sequence number to JENKINS_HOME five times
# a second, each write fsynced. After a takeover the file says which writes survived and when
# the last one was made. Jenkins' own start (~17 s in the lab) does not depend on how many
# volumes move at once, and a dozen of these fit where one Jenkins does not.
set -u
h=/var/jenkins_home
now() { adjtimex | awk '/tv_sec/{s=$2} /tv_usec/{u=$2} END{printf "%d.%09d", s, u}'; }
append() { echo "$2" | dd of="$h/$1" oflag=append conv=notrunc,fsync 2>/dev/null; }
n=$(tail -n 1 "$h/seq" 2>/dev/null | cut -d' ' -f1)
n=${n:-0}
append starts "$(now) $n"
mkdir -p /tmp/www && echo ok > /tmp/www/login
httpd -f -p 8080 -h /tmp/www &
while :; do
  n=$((n + 1))
  append seq "$n $(now)"
  sleep 0.2
done

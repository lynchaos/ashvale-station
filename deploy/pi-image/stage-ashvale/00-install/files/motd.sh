#!/bin/sh
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
printf '\n  Ashvale Station  ->  http://%s:8000\n' "${IP:-<this-pi>}"
printf '  status: %s\n' "$(systemctl is-active ashvale 2>/dev/null || echo unknown)"
printf '\n  No authentication and no TLS. Trusted LAN only: do not port-forward it.\n'
printf '  Set your coordinates and altitude on the Settings tab before trusting\n'
printf '  the pressure readings. See /opt/ashvale/README.first-boot\n\n'

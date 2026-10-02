#!/bin/sh
# Render the egress allowlist from EGRESS_ALLOW, start the ingress relays, run tinyproxy.
set -eu
: "${EGRESS_ALLOW:?EGRESS_ALLOW must list the hostnames the agent may reach}"
: "${INGRESS_TARGET:=hermes}"

printf '%s\n' "$EGRESS_ALLOW" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' > /etc/tinyproxy/filter
echo "[edge] egress allowlist:"; sed 's/^/[edge]   /' /etc/tinyproxy/filter

# Ingress: publish the agent's dashboard and API on the host side without giving the agent
# container a published port (it lives on an internal network).
socat TCP-LISTEN:9119,fork,reuseaddr TCP:"${INGRESS_TARGET}":9119 &
socat TCP-LISTEN:8642,fork,reuseaddr TCP:"${INGRESS_TARGET}":8642 &

exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf

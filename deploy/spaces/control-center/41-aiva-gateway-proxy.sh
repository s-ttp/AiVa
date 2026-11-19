#!/bin/sh
# Writes /var/run/aiva/gateway-proxy.conf (the server-side /gateway/ proxy to the private gateway Space)
# from AIVA_GATEWAY_UPSTREAM and the HF_TOKEN Space secret. Run by the nginx image's entrypoint.
exec /usr/local/bin/aiva-gateway-proxy /var/run/aiva/gateway-proxy.conf

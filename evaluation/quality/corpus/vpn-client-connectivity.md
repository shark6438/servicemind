# VPN client connectivity recovery

Applies to the managed VPN client on corporate laptops. Owned by the Network Team.

## Symptoms
A session that was working stops completing the tunnel handshake, or completes it and
carries no traffic. The client log shows `TLS handshake timeout` or `no route to peer`.

## Procedure
1. Confirm the client version is 7.4 or later. Versions below 7.4 cannot negotiate the
   current TLS profile and fail with a handshake timeout that looks like a network fault.
2. Confirm the laptop holds a valid device certificate issued within the last 400 days.
3. Clear the client state directory `/var/lib/vpn-client/state` and restart the service.
4. Reconnect once. A second failure within five minutes is not retried automatically.
5. If the handshake still fails, capture the client log and open a Network Team ticket.

## Escalation
Unresolved after step 5 escalates to the Network Team queue with 4 hour response.

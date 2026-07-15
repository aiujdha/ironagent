# prod-single-entry-nodeport

This overlay is for validating the single public entry model.

External NodePort exposure:

- `go-gateway`: `30808`

Internal-only services:

- `ironagent-gateway`
- `admin`
- `security-service`

The control plane still keeps internal service URLs for southbound calls, while browser assistant UI links are generated through:

```text
http://<PUBLIC_HOST>:30808/assistants/{assistant_id}/ui/?launchTicket=...
```

Use this overlay only after Go Gateway Runtime Proxy and launch ticket/session are enabled.

Before rendering or applying this overlay, run:

```bash
sh scripts/check_single_entry_exposure.sh
```

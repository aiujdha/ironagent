`callbackUrl` 现在不是由 `POST /api/v1/chat` 请求体传入，而是在 **Go Gateway 的部署配置里配置**。

当前代码里有两个配置入口：

1. **文件配置 ConfigMap**

路径：

`deployment/kustomize/ironagent/base/go-gateway-file-configmap.yaml`

字段：

```json
{
  "default_callback_url": "",
  "callback_allowed_hosts": [],
  "callback_allow_private_hosts": false
}
```

这里的 `default_callback_url` 就是默认 callback 地址。

2. **环境变量配置 ConfigMap**

路径：

`deployment/kustomize/ironagent/base/go-gateway-configmap.yaml`

相关字段：

```yaml
GO_GATEWAY_CALLBACK_ALLOWED_HOSTS: ""
GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS: "false"
```

代码也支持环境变量：

```text
GO_GATEWAY_DEFAULT_CALLBACK_URL
GO_GATEWAY_CALLBACK_ALLOWED_HOSTS
GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS
```

环境变量会覆盖 JSON 文件配置。

实际推荐配置方式：

```yaml
GO_GATEWAY_DEFAULT_CALLBACK_URL: "http://<上游服务地址>/intelligent-assistant/h5/iron/taskcallback"
GO_GATEWAY_CALLBACK_ALLOWED_HOSTS: "<上游服务域名或IP>"
GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS: "false"
```

如果是内网联调，比如 callback 地址是内网 IP，需要临时设置：

```yaml
GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS: "true"
```

生产环境不要打开这个。

线上查看当前配置：

```bash
kubectl -n ironagent get cm go-gateway-file-config -o yaml
kubectl -n ironagent get cm go-gateway-config -o yaml
```

如果修改 ConfigMap 后，需要重启 Gateway：

```bash
kubectl -n ironagent rollout restart deploy/go-gateway
kubectl -n ironagent rollout status deploy/go-gateway --timeout=300s
```

一句话：  
**callbackUrl 配在 Go Gateway 的 `default_callback_url` 或 `GO_GATEWAY_DEFAULT_CALLBACK_URL`，不是上游 chat 请求参数。**
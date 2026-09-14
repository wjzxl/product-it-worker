# 编码/测试沙箱镜像

AI 生成的代码（研发域编码、测试域独立用例）**只在此容器内执行**，做到：

- `--network none`：执行期无任何网络（无法外联、无法窃取 token）
- `--memory/--cpus/--pids-limit`：资源限额
- `--user <uid>:<gid>`：以宿主用户身份运行，避免产物归属 root
- `--rm`：用后即毁

构建：
```bash
docker build -t platform-sandbox:latest platform/sandbox
```

使用（agent-runtime 自动）：
```bash
export SANDBOX_MODE=docker          # 默认 host（本地无 docker 时回退）
export SANDBOX_IMAGE=platform-sandbox:latest
```

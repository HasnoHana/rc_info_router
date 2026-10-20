# 内部通知投递服务 —— 本地命令
# Windows 无 make 时可直接执行各目标下的命令（见 README）。

.PHONY: verify run mock-vendor sessions clean

# 全量验证：单元/集成测试 + 端到端冒烟（DoD 的"全绿"标准）
verify:
	python -m unittest discover -s tests
	python scripts/e2e_smoke.py

# 启动服务（API + worker 同进程）；数据库路径可用 NDS_DB_PATH 覆盖
run:
	python -m app.main

# 本地模拟供应商（另开终端）
mock-vendor:
	python scripts/mock_vendor.py 9901

# 打包 AI 会话归档（交付物之一：ai-sessions.zip）
sessions:
	powershell -NoProfile -Command "Compress-Archive -Path 'docs/ai/*' -DestinationPath 'ai-sessions.zip' -Force"

clean:
	powershell -NoProfile -Command "Remove-Item -Recurse -Force data, ai-sessions.zip -ErrorAction SilentlyContinue"

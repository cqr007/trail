module.exports = {
  apps : [{
    name: "hl-bot",
    script: "./chua_Hyperliquid.py",
    cwd: "./",                 // 强制当前目录为工作目录
    interpreter: "python3",    // 指定解释器
    env: {
      PYTHONUNBUFFERED: "1"    // 确保日志实时输出
    }
  }]
}
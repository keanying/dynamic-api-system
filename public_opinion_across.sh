#!/bin/bash

# 完整的 uv run 管理脚本
# 保存为 uv_manager.sh

APP_DIR="$(dirname "$0")"
PID_FILE="$APP_DIR/logs/public_opinion_across.pid"
LOG_FILE="$APP_DIR/logs/public_opinion_across.log"
UV_COMMAND="env APP_ENV=pro uv run uvicorn app.main:app --host 0.0.0.0 --port 3000  --workers 5"  # 修改为你的实际命令

start() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "应用已在运行中，PID: $(cat $PID_FILE)"
        return 1
    fi

    echo "正在启动应用..."
    cd "$APP_DIR"

    # 后台运行
    nohup $UV_COMMAND > "$LOG_FILE" 2>&1 &

    # 保存PID
    echo $! > "$PID_FILE"
    echo "应用已启动，PID: $(cat $PID_FILE)"
    echo "日志文件: $LOG_FILE"
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "未找到PID文件，应用可能未运行"
        return 1
    fi

    PID=$(cat "$PID_FILE")
    if kill -0 $PID 2>/dev/null; then
        echo "正在停止应用，PID: $PID"
        kill $PID
        sleep 2

        # 检查是否成功停止
        if kill -0 $PID 2>/dev/null; then
            echo "强制终止应用..."
            kill -9 $PID
        fi

        rm "$PID_FILE"
        echo "应用已停止"
    else
        echo "应用未运行"
        rm "$PID_FILE"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "应用运行中，PID: $(cat $PID_FILE)"
        echo "日志最后10行:"
        tail -10 "$LOG_FILE"
    else
        echo "应用未运行"
    fi
}

logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "日志文件不存在"
    fi
}

case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        sleep 2
        start
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac  # 添加这个结束标记
global:
  resolve_timeout: 5m

route:
  receiver: telegram
  group_by: [alertname, instance]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - match:
        alertname: E2ETestFailed
      group_by: [alertname, bot]
      repeat_interval: 1h

receivers:
  - name: telegram
    telegram_configs:
      - bot_token: "${TG_BOT_TOKEN}"
        api_url: https://api.telegram.org
        chat_id: ${TG_CHAT_ID}
        send_resolved: true

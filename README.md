## hermes agent plugin for synology-chat   

--- 

### this is only a plugin, not any modify hermes codes

--
  
### usage:   
  1. create dirctory in ~/.hermes/plugins, such as : ~/.hermes/plugins/synology-chat
     
  2. place files into ~/.hermes/plugins/synology-chat:   
        ```__init__.py plugin.yaml adapter.py```
     
  3. modify ~/.hermes/config.yaml like these:
     
       ```javascript
      platforms:
        synology_chat:
          enabled: true
          extra:
            api_endpoint: http://NASIP:5000/webapi/entry.cgi?api=SYNO.Chat.External&method=chatbot&version=2&token=%22........%22
            host: 0.0.0.0
            port: 8086
            ssl_verify: false
            webhook_path: /synology-chat/webhook
          token: ..........
      ```   
      
  4. modify ~/.hermes/.env like these:
      ```javascript
      # ==========================================
      # 选项 A：允许特定群晖用户（推荐）
      # ==========================================
      # 填入允许访问机器人的群晖 Chat user_id（通常是数字，逗号分隔）
      # 如果不在列表中的人发消息，Hermes 会自动拦截并触发配对流程，向其发送配对码
      SYNOLOGY_CHAT_ALLOWED_USERS=123,456,789
      
      # ==========================================
      # 选项 B：允许所有用户（二选一）
      # ==========================================
      # 如果你想让全公司/内网全员都能直接免配对使用，请开启此项
      # SYNOLOGY_CHAT_ALLOW_ALL_USERS=true
      
      # ==========================================
      # 定时任务 (Cron) 目标接收人
      # ==========================================
      # 当你在后台配置了 Cron 定时提醒任务（比如每日早报、服务器告警）且没指定目的地时，
      # Hermes 会默认把通知推送到这个群晖 user_id
      SYNOLOGY_CHAT_HOME_CHANNEL=123
      ```   

  5. restart hermes-agent
  
### notice:   
--  if you want not use config.yaml, you can also setting .env with all config parameters like there:   

      ```javascript
      # 只有在你不使用 config.yaml 时，才需要配置以下变量：
      SYNOLOGY_CHAT_TOKEN=vo3n.........
      SYNOLOGY_CHAT_API_ENDPOINT=http://192.168.1.1:5000/webapi/entry.cgi?api=SYNO.Chat.External&method=chatbot&version=2&token=%22vo3.........QuSXJ%22
      SYNOLOGY_CHAT_HOST=0.0.0.0
      SYNOLOGY_CHAT_PORT=8086
      SYNOLOGY_CHAT_SSL_VERIFY=false
      SYNOLOGY_CHAT_WEBHOOK_PATH=/synology-chat/webhook
      ```   
-- a manual from synology KB   
  https://kb.synology.com/sv-se/DSM/help/Chat/chat_integration?version=7   

-- synology chat server spk download:   
  https://archive.synology.com/download/Package/Chat/   

-- 
  

      

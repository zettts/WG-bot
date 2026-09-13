PATH = "src/handlers.py"

with open(PATH, "r", encoding="utf-8") as f:
    content = f.read()

OLD = """                # Обновляем expiry_time в 3x-ui если у пользователя есть профиль
                if user.vless_profile_data:
                    try:
                        profile_data = safe_json_loads(user.vless_profile_data, default={})
                        email = profile_data.get("email")
                        if email:
                            expiry_time = get_safe_expiry_timestamp(user.subscription_end)
                            logger.info(f"📅 Admin remove time for user {user_id}: {expiry_time}")
                            await update_client_expiry(email, expiry_time)
                            logger.info(f"✅ Updated expiry time in 3x-ui for user {user_id} (admin remove time)")
                    except Exception as e:
                        logger.error(f"🛑 Failed to update expiry time in 3x-ui for user {user_id}: {e}")
"""

NEW = ""

count = content.count(OLD)
print(f"Найдено совпадений: {count}")

if count >= 1:
    content = content.replace(OLD, NEW)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Удалено {count} совпадений.")

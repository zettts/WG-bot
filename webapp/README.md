# Telegram Mini App

Открывается по ссылке t.me/GetTopVPN_bot/app

## Деплой

    sudo cp webapp/index.html webapp/logo.png /var/www/topvpn.site/app/
    sudo chown ubuntu:www-data /var/www/topvpn.site/app/*

## Как работает

Пользователь выбирает тариф и устройство, жмёт «Подключить».
Приложение открывает t.me/GetTopVPN_bot?start=buy_<months>_<platform>,
бот ловит параметр в start_cmd и выставляет счёт или шлёт инструкцию.

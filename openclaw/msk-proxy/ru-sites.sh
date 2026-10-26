#!/bin/bash
# Браузер Джарвиса: российские сайты через московский туннель, всё остальное напрямую (Нидерланды).
#
# Запуск на сервере Джарвиса под jarvis:
#   bash ~/openclaw/msk-proxy/ru-sites.sh
# Чтобы добавить сайт: допиши его в RU_DOMAINS ниже и запусти скрипт снова.
set -euo pipefail

PORT=1080
# Домены, которые идут через Москву (вместе со всеми поддоменами).
RU_DOMAINS="ru su xn--p1ai avito.ru ozon.ru ozon.com yandex.com ya.ru yandex.net wildberries.ru wb.ru 2gis.com 2gis.ru gosuslugi.ru vk.com vk.ru mail.ru ok.ru hh.ru dzen.ru rutube.ru kinopoisk.ru sber.ru sberbank.ru tbank.ru tinkoff.ru megamarket.ru lamoda.ru dns-shop.ru market.yandex.ru drom.ru auto.ru cian.ru"

LIST=""
for d in $RU_DOMAINS; do LIST="$LIST\"$d\","; done
LIST="[${LIST%,}]"

PAC="function FindProxyForURL(url, host) {
  host = host.toLowerCase();
  var ru = $LIST;
  for (var i = 0; i < ru.length; i++) {
    var d = ru[i];
    if (host === d || dnsDomainIs(host, '.' + d)) return 'SOCKS5 127.0.0.1:$PORT; DIRECT';
  }
  return 'DIRECT';
}"

B64="$(printf '%s' "$PAC" | base64 -w0)"
openclaw config set browser.extraArgs "[\"--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,$B64\"]" --strict-json
echo "Готово. Через Москву: $RU_DOMAINS"
echo "Перезапусти Джарвиса: openclaw gateway restart"

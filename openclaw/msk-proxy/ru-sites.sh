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

# Внутренние адреса сервера уходят «в никуда» (закрытый порт 9): браузер не достанет
# до «Сводки», панели OpenClaw и других служб на сервере, даже если страница попросит.
PAC="function FindProxyForURL(url, host) {
  host = host.toLowerCase().replace(/^\\[|\\]$/g, '');
  if (host === 'localhost' || dnsDomainIs(host, '.localhost') || isPlainHostName(host) ||
      host === '::1' || host === '0.0.0.0' || shExpMatch(host, '127.*') || shExpMatch(host, '10.*') ||
      shExpMatch(host, '192.168.*') || shExpMatch(host, '169.254.*') || shExpMatch(host, 'fc*:*') ||
      shExpMatch(host, 'fd*:*') || shExpMatch(host, 'fe80:*') ||
      /^172\\.(1[6-9]|2[0-9]|3[01])\\./.test(host) || /^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\./.test(host)) {
    return 'PROXY 127.0.0.1:9';
  }
  var ru = $LIST;
  for (var i = 0; i < ru.length; i++) {
    var d = ru[i];
    if (host === d || dnsDomainIs(host, '.' + d)) return 'SOCKS5 127.0.0.1:$PORT; DIRECT';
  }
  return 'DIRECT';
}"

B64="$(printf '%s' "$PAC" | base64 -w0)"
# <-loopback> заставляет Chrome спрашивать правила и для localhost (по умолчанию он их обходит).
openclaw config set browser.extraArgs "[\"--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,$B64\",\"--proxy-bypass-list=<-loopback>\"]" --strict-json
# Без этого OpenClaw не пускает браузер через прокси. Внутренние адреса закрыты правилами выше.
openclaw config set browser.ssrfPolicy.dangerouslyAllowPrivateNetwork true --strict-json
echo "Готово. Через Москву: $RU_DOMAINS"
echo "Перезапусти Джарвиса: openclaw gateway restart"

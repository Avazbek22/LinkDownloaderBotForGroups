from __future__ import annotations

from typing import Any

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "private_help": (
            "<b>Video downloader for Telegram groups</b>\n\n"
            "1. Add me to a group.\n"
            "2. Disable Group Privacy in BotFather.\n"
            "3. Grant permission to delete messages.\n\n"
            "Post a video link and I will quietly replace it with the video.\n\n"
            "Use /language en or /language ru to change the language.\n"
            'Source: <a href="{repo_url}">GitHub</a>'
        ),
        "group_help": (
            "<b>How to use</b>\n"
            "Post a video link. 👀 means it is being processed; 👎 means it failed. "
            "I will publish the video silently and delete the original message after success.\n\n"
            "<b>Personal opt-out</b>\n"
            "Send {bot_mention} me to toggle automatic downloads for yourself.\n"
            "When disabled, use {bot_mention} &lt;link&gt;.\n\n"
            "Administrators can change the language with /language en or /language ru."
        ),
        "admin_hint": (
            "⚠️ <b>Administrator permission recommended</b>\n"
            "Grant permission to delete messages so I can remove original links after a successful upload.\n\n"
        ),
        "already_welcomed": "The instructions were already sent. Use /help to show them again.",
        "private_hint": "Use /help to see the instructions.",
        "opted_out": (
            "{who}, automatic downloads are now disabled for you.\n"
            "Mention {bot_mention} together with a link to download it."
        ),
        "opted_in": "{who}, automatic downloads are enabled again. You can simply post links.",
        "language_current": "Current language: {language}. Available: en, ru.",
        "language_changed": "Language changed to English.",
        "language_admin_only": "Only group administrators can change the language.",
        "language_invalid": "Supported languages: en, ru.",
        "admin_only": "Only group administrators can change this setting.",
        "settings_summary": "<b>Group settings</b>\nLanguage: {language}\nDelete original link: {delete_original}",
        "delete_usage": "Use /delete_original on or /delete_original off.",
        "delete_changed": "Deleting original links is now {state}.",
        "state_on": "enabled",
        "state_off": "disabled",
        "caption": '<a href="{url}">Original video · {source}</a>\nFrom {sender}',
    },
    "ru": {
        "private_help": (
            "<b>Бот для скачивания видео в группах Telegram</b>\n\n"
            "1. Добавьте меня в группу.\n"
            "2. Отключите Group Privacy в BotFather.\n"
            "3. Разрешите удалять сообщения.\n\n"
            "Отправьте ссылку на видео — я тихо заменю её готовым видео.\n\n"
            "Язык: /language en или /language ru.\n"
            'Исходный код: <a href="{repo_url}">GitHub</a>'
        ),
        "group_help": (
            "<b>Как пользоваться</b>\n"
            "Отправьте ссылку на видео. 👀 означает, что ссылка обрабатывается, а 👎 — что скачать не удалось. "
            "Я тихо опубликую видео, а после успеха удалю исходное сообщение.\n\n"
            "<b>Персональное отключение</b>\n"
            "Отправьте {bot_mention} я, чтобы отключить или включить автоматическое скачивание для себя.\n"
            "Когда оно отключено, используйте {bot_mention} &lt;ссылка&gt;.\n\n"
            "Администраторы могут изменить язык: /language en или /language ru."
        ),
        "admin_hint": (
            "⚠️ <b>Рекомендуются права администратора</b>\n"
            "Разрешите удалять сообщения, чтобы я удалял исходные ссылки после успешной отправки.\n\n"
        ),
        "already_welcomed": "Инструкция уже отправлялась. Используйте /help, чтобы показать её снова.",
        "private_hint": "Используйте /help, чтобы увидеть инструкцию.",
        "opted_out": (
            "{who}, автоматическое скачивание для Вас отключено.\n"
            "Для загрузки упомяните {bot_mention} вместе со ссылкой."
        ),
        "opted_in": "{who}, автоматическое скачивание снова включено. Можно просто отправлять ссылки.",
        "language_current": "Текущий язык: {language}. Доступны: en, ru.",
        "language_changed": "Язык изменён на русский.",
        "language_admin_only": "Изменять язык могут только администраторы группы.",
        "language_invalid": "Поддерживаемые языки: en, ru.",
        "admin_only": "Изменять эту настройку могут только администраторы группы.",
        "settings_summary": "<b>Настройки группы</b>\nЯзык: {language}\nУдаление исходной ссылки: {delete_original}",
        "delete_usage": "Используйте /delete_original on или /delete_original off.",
        "delete_changed": "Удаление исходных ссылок теперь {state}.",
        "state_on": "включено",
        "state_off": "выключено",
        "caption": '<a href="{url}">Ссылка на видео · {source}</a>\nОт {sender}',
    },
}


def tr(locale: str, key: str, **values: Any) -> str:
    catalog = TEXTS.get(locale, TEXTS["en"])
    template = catalog.get(key, TEXTS["en"].get(key, key))
    return template.format(**values)

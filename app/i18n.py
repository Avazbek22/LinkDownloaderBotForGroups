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
            "Use /en or /ru to change the language.\n"
            'Source: <a href="{repo_url}">GitHub</a>'
        ),
        "group_help": (
            "<b>How to use</b>\n"
            "Post a link for video, or use /audio &lt;link&gt; for MP3. "
            "👀 means it is being processed, 🙈 means Instagram hid or restricted the content, "
            "😴 means Instagram or YouTube temporarily limited the bot, "
            "🤷 means the requested media was not found, and 👎 means it failed. "
            "Add the same 🙈, 😴, or 👎 reaction yourself to retry; 😴 retries never bypass the cooldown. "
            "I will publish the result silently and delete the original message after success.\n\n"
            "Use /skip &lt;link&gt; to leave one link completely untouched.\n\n"
            "<b>Personal opt-out</b>\n"
            "Send {bot_mention} me to toggle automatic downloads for yourself.\n"
            "When disabled, use {bot_mention} &lt;link&gt;.\n\n"
            "Administrators can change the language with /en or /ru."
        ),
        "admin_hint": (
            "⚠️ <b>Administrator permission recommended</b>\n"
            "Grant permission to delete messages so I can remove original links after a successful upload.\n\n"
        ),
        "admin_ready": ("✅ <b>The bot is already an administrator</b>\nPermission to delete messages is enabled.\n\n"),
        "admin_delete_missing": (
            "✅ <b>The bot is already an administrator</b>\n"
            "⚠️ Enable permission to delete messages so I can remove original links after a successful upload.\n\n"
        ),
        "admin_delete_unverified": (
            "✅ <b>The bot is already an administrator</b>\n"
            "⚠️ I could not verify the permission to delete messages right now.\n\n"
        ),
        "admin_status_unverified": (
            "✅ <b>Administrator rights were previously confirmed</b>\n"
            "⚠️ I could not verify the current permissions right now.\n\n"
        ),
        "already_welcomed": "The instructions were already sent. Use /help to show them again.",
        "private_hint": "Use /help to see the instructions.",
        "opted_out": (
            "{who}, automatic downloads are now disabled for you.\n"
            "Mention {bot_mention} together with a link to download it."
        ),
        "opted_in": "{who}, automatic downloads are enabled again. You can simply post links.",
        "language_changed": "Language changed to English.",
        "language_admin_only": "Only group administrators can change the language.",
        "admin_only": "Only group administrators can change this setting.",
        "settings_summary": "<b>Group settings</b>\nLanguage: {language}\nDelete original link: {delete_original}",
        "delete_usage": "Use /delete_original on or /delete_original off.",
        "audio_usage": "Use /audio <link> to download one link as MP3.",
        "delete_changed": "Deleting original links is now {state}.",
        "state_on": "enabled",
        "state_off": "disabled",
        "group_pending_approval": (
            "⏳ This group is waiting for the bot owner's approval. Media links will not be processed yet."
        ),
        "group_approved": "✅ This group was approved. Media downloads are now enabled.",
        "group_rejected": "⛔ The bot owner did not approve this group. I am leaving the group.",
        "group_approval_expired": "⌛ Approval was not received in time. I am leaving the group.",
        "group_access_revoked": "⛔ The bot owner revoked access to this group. I am leaving the group.",
        "caption": '<a href="{url}">Original video · {source}</a>\nFrom {sender}',
        "audio_caption": '<a href="{url}">Original audio · {source}</a>\nFrom {sender}',
    },
    "ru": {
        "private_help": (
            "<b>Бот для скачивания видео в группах Telegram</b>\n\n"
            "1. Добавьте меня в группу.\n"
            "2. Отключите Group Privacy в BotFather.\n"
            "3. Разрешите удалять сообщения.\n\n"
            "Отправьте ссылку на видео — я тихо заменю её готовым видео.\n\n"
            "Язык: /en или /ru.\n"
            'Исходный код: <a href="{repo_url}">GitHub</a>'
        ),
        "group_help": (
            "<b>Как пользоваться</b>\n"
            "Отправьте ссылку для скачивания видео или используйте /audio &lt;ссылка&gt; для MP3. "
            "👀 означает, что ссылка обрабатывается, 🙈 — что Instagram скрыл или "
            "ограничил контент, 😴 — что Instagram или YouTube временно ограничил бота, "
            "🤷 — что нужное медиа не найдено, а 👎 — что скачать не удалось. "
            "Добавьте такую же реакцию 🙈, 😴 или 👎, чтобы повторить загрузку; "
            "повтор с 😴 не обходит защитную паузу. "
            "Я тихо опубликую результат, а после успеха удалю исходное сообщение.\n\n"
            "Используйте /skip &lt;ссылка&gt;, чтобы один раз полностью проигнорировать ссылку.\n\n"
            "<b>Персональное отключение</b>\n"
            "Отправьте {bot_mention} я, чтобы отключить или включить автоматическое скачивание для себя.\n"
            "Когда оно отключено, используйте {bot_mention} &lt;ссылка&gt;.\n\n"
            "Администраторы могут изменить язык: /en или /ru."
        ),
        "admin_hint": (
            "⚠️ <b>Рекомендуются права администратора</b>\n"
            "Разрешите удалять сообщения, чтобы я удалял исходные ссылки после успешной отправки.\n\n"
        ),
        "admin_ready": ("✅ <b>Бот уже является администратором</b>\nРазрешение на удаление сообщений включено.\n\n"),
        "admin_delete_missing": (
            "✅ <b>Бот уже является администратором</b>\n"
            "⚠️ Разрешите удалять сообщения, чтобы я удалял исходные ссылки после успешной отправки.\n\n"
        ),
        "admin_delete_unverified": (
            "✅ <b>Бот уже является администратором</b>\n"
            "⚠️ Сейчас не удалось проверить разрешение на удаление сообщений.\n\n"
        ),
        "admin_status_unverified": (
            "✅ <b>Права администратора были подтверждены ранее</b>\n"
            "⚠️ Сейчас не удалось проверить актуальные разрешения.\n\n"
        ),
        "already_welcomed": "Инструкция уже отправлялась. Используйте /help, чтобы показать её снова.",
        "private_hint": "Используйте /help, чтобы увидеть инструкцию.",
        "opted_out": (
            "{who}, автоматическое скачивание для Вас отключено.\n"
            "Для загрузки упомяните {bot_mention} вместе со ссылкой."
        ),
        "opted_in": "{who}, автоматическое скачивание снова включено. Можно просто отправлять ссылки.",
        "language_changed": "Язык изменён на русский.",
        "language_admin_only": "Изменять язык могут только администраторы группы.",
        "admin_only": "Изменять эту настройку могут только администраторы группы.",
        "settings_summary": "<b>Настройки группы</b>\nЯзык: {language}\nУдаление исходной ссылки: {delete_original}",
        "delete_usage": "Используйте /delete_original on или /delete_original off.",
        "audio_usage": "Используйте /audio <ссылка>, чтобы скачать одну ссылку в MP3.",
        "delete_changed": "Удаление исходных ссылок теперь {state}.",
        "state_on": "включено",
        "state_off": "выключено",
        "group_pending_approval": (
            "⏳ Эта группа ожидает подтверждения владельца бота. До подтверждения ссылки обрабатываться не будут."
        ),
        "group_approved": "✅ Группа подтверждена. Скачивание медиа теперь включено.",
        "group_rejected": "⛔ Владелец бота не подтвердил эту группу. Я покидаю группу.",
        "group_approval_expired": "⌛ Подтверждение не получено вовремя. Я покидаю группу.",
        "group_access_revoked": "⛔ Владелец бота отозвал доступ для этой группы. Я покидаю группу.",
        "caption": '<a href="{url}">Ссылка на видео · {source}</a>\nОт {sender}',
        "audio_caption": '<a href="{url}">Ссылка на аудио · {source}</a>\nОт {sender}',
    },
}


def tr(locale: str, key: str, **values: Any) -> str:
    catalog = TEXTS.get(locale, TEXTS["en"])
    template = catalog.get(key, TEXTS["en"].get(key, key))
    return template.format(**values)

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
            "Post a link and I will silently publish the video, then delete the original message.\n"
            "For MP3, use /audio &lt;link&gt;.\n\n"
            "<b>Reactions</b>\n"
            "👀 — the link is being processed\n"
            "🙈 — Instagram hid or restricted the content\n"
            "😴 — Instagram or YouTube temporarily limited the bot\n"
            "🤷 — the requested media was not found\n"
            "👎 — the download failed\n"
            "👍 — the media was posted, but the link was kept\n\n"
            "<b>Retry</b>\n"
            "Add the same 🙈, 😴, or 👎 reaction yourself to try again.\n"
            "😴 retries never bypass the cooldown.\n\n"
            "<b>Leave a link alone</b>\n"
            "/skip &lt;link&gt; — I will not touch this link at all.\n"
            "Changed your mind while 👀 is shown? Add 👀 to your own link and I will stop and leave it as it is.\n\n"
            "<b>Personal opt-out</b>\n"
            "{bot_mention} me — turn automatic downloads off or on for yourself.\n"
            "While they are off, use {bot_mention} &lt;link&gt;.\n\n"
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
        "auto_download_off_question": "{who}, turn off auto-download for your links?",
        "auto_download_on_question": "{who}, turn auto-download for your links back on?",
        "auto_download_off_done": "Done: auto-download for your links is off.",
        "auto_download_on_done": "Done: auto-download for your links is back on.",
        "auto_download_not_yours": "This question isn't for you.",
        "yes": "Yes",
        "no": "No",
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
            "Отправьте ссылку — я тихо опубликую видео, а потом удалю исходное сообщение.\n"
            "Для MP3 используйте /audio &lt;ссылка&gt;.\n\n"
            "<b>Реакции</b>\n"
            "👀 — ссылка обрабатывается\n"
            "🙈 — Instagram скрыл или ограничил контент\n"
            "😴 — Instagram или YouTube временно ограничил бота\n"
            "🤷 — нужное медиа не найдено\n"
            "👎 — скачать не удалось\n"
            "👍 — медиа опубликовано, но ссылка оставлена\n\n"
            "<b>Повтор</b>\n"
            "Поставьте такую же реакцию 🙈, 😴 или 👎, чтобы повторить загрузку.\n"
            "Повтор с 😴 не обходит защитную паузу.\n\n"
            "<b>Не трогать ссылку</b>\n"
            "/skip &lt;ссылка&gt; — я вообще не трону эту ссылку.\n"
            "Передумали, пока стоит 👀? Поставьте 👀 на свою ссылку — я остановлюсь и оставлю её как есть.\n\n"
            "<b>Персональное отключение</b>\n"
            "{bot_mention} я — отключить или включить автоскачивание для себя.\n"
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
        "auto_download_off_question": "{who}, отключить автоскачивание ваших ссылок?",
        "auto_download_on_question": "{who}, снова включить автоскачивание ваших ссылок?",
        "auto_download_off_done": "Готово: автоскачивание ваших ссылок отключено.",
        "auto_download_on_done": "Готово: автоскачивание ваших ссылок снова включено.",
        "auto_download_not_yours": "Этот вопрос не для вас.",
        "yes": "Да",
        "no": "Нет",
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

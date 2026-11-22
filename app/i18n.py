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
            "Send a video link — I'll download the video, post it here and delete the message with the link.\n"
            "Only need the sound? Send /audio with the link — I'll post an MP3.\n\n"
            "<b>What the reactions mean</b>\n"
            "👀 — downloading\n"
            "👍 — video posted, link kept\n"
            "👎 — the download failed\n"
            "🙈 — Instagram doesn't allow downloading this video\n"
            "😴 — YouTube or Instagram is temporarily blocking downloads, please wait\n"
            "🤷 — there is no video at this link\n\n"
            "<b>Download again</b>\n"
            "Add the same reaction I left on the link: 👎, 🙈 or 😴.\n"
            "If it shows 😴, I'll download only after the pause is over.\n\n"
            "<b>Don't want it downloaded?</b>\n"
            "Put /skip before the link — I won't download it.\n"
            "Already sent the link and see 👀? Add 👀 too — I'll cancel the download and keep the link.\n\n"
            "<b>Auto-download for you</b>\n"
            "Mention me in a separate message — I'll offer to turn auto-download of your links off or back on.\n"
            "While it's off, mention me together with a link — and I'll download the video.\n\n"
            "Change language: /en or /ru (admins only)."
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
            "Отправьте ссылку на видео — я скачаю его, пришлю в чат и удалю сообщение со ссылкой.\n"
            "Нужен только звук? Напишите /audio и ссылку — пришлю MP3.\n\n"
            "<b>Что значат реакции на ссылке</b>\n"
            "👀 — скачиваю\n"
            "👍 — видео отправил, ссылку оставил\n"
            "👎 — не получилось скачать\n"
            "🙈 — Instagram не даёт скачать это видео\n"
            "😴 — YouTube или Instagram временно не дают скачивать, нужно подождать\n"
            "🤷 — по ссылке нет видео\n\n"
            "<b>Как скачать ещё раз</b>\n"
            "Поставьте на ссылку ту же реакцию, что поставил я: 👎, 🙈 или 😴.\n"
            "Если стоит 😴, скачаю только после того, как пауза закончится.\n\n"
            "<b>Если скачивать не нужно</b>\n"
            "Напишите /skip перед ссылкой — эту ссылку я не скачаю.\n"
            "Уже отправили ссылку и стоит 👀? Поставьте тоже 👀 — отменю скачивание, ссылка останется.\n\n"
            "<b>Автоскачивание для себя</b>\n"
            "Отметьте меня отдельным сообщением — я предложу отключить или снова включить автоскачивание "
            "ваших ссылок.\n"
            "Пока оно отключено, отметьте меня вместе со ссылкой — и я скачаю видео.\n\n"
            "Сменить язык: /en или /ru (только администраторы)."
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

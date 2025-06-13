from __future__ import annotations

import json
import os
from typing import ClassVar, Final, TypeAlias, TYPE_CHECKING

from discord import Locale, app_commands
from discord.app_commands import TranslationContextLocation, TranslationContextTypes, locale_str

from config import beta

if TYPE_CHECKING:
    from app.core import Bot

    TranslationDictValue: TypeAlias = str | 'TranslationDict' | list['TranslationDictValue']
    TranslationDict: TypeAlias = dict[str, TranslationDictValue]


class Translator(app_commands.Translator):
    BASE_DIR: Final[ClassVar[str]] = 'assets/translations'
    BASE_LOCALE: Final[ClassVar[Locale]] = Locale.american_english

    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self._translations: dict[Locale, TranslationDict] = {}
        self._bot: Bot = bot
        # discord.py doesn't provide the parent parameter of each choice
        self.__choice_to_param_mapping: dict[int, app_commands.Parameter] = {}

    def update_base_translations(self) -> None:
        """Ensures the base locale file exists, then writes updated command strings to the base file."""
        if not beta:
            return  # this is to prevent git conflicts

        os.makedirs(self.BASE_DIR, exist_ok=True)
        base_locale_path = os.path.join(self.BASE_DIR, f'{self.BASE_LOCALE}.json')

        with open(base_locale_path, 'w+', encoding='utf-8') as f:
            try:
                raw = json.loads(f.read())
            except json.JSONDecodeError:
                raw = {}

            raw['commands'] = {
                command.qualified_name.replace(' ', '_'): {
                    'name': command.name,
                    'description': command.description,
                    **(
                        dict(options={
                            param.name: {
                                'name': param.display_name,
                                'description': param.description,
                                **(
                                    dict(choices={choice.value: choice.name for choice in param.choices})
                                    if param.choices else {}
                                )
                            }
                            for param in command.parameters
                        }) if getattr(command, 'parameters', None) else {}
                    )
                }
                for command in self._bot.tree.walk_commands()
            }

            f.seek(0)
            f.truncate()
            f.write(json.dumps(raw, indent=2))

    async def load(self) -> None:
        """Load translations from the base locale file."""
        self.update_base_translations()

        for file in os.listdir(self.BASE_DIR):
            if file.endswith('.json'):
                locale = Locale(file[:-5])
                with open(os.path.join(self.BASE_DIR, file), 'r', encoding='utf-8') as f:
                    self._translations[locale] = json.load(f)

    async def translate(self, string: locale_str, locale: Locale, context: TranslationContextTypes) -> str | None:
        if context.location is TranslationContextLocation.other:
            return None  # TODO

        if locale not in self._translations:
            return None

        command = context.data
        param = choice = None
        if isinstance(command, app_commands.Parameter):
            param = command
            command = param.command
            # prepare a lookup from id(choice) -> parent parameter
            for choice in param.choices:
                self.__choice_to_param_mapping[id(choice)] = param
        elif isinstance(command, app_commands.Choice):
            choice = command
            param = self.__choice_to_param_mapping.get(id(choice))
            command = param and param.command

        if not command or not isinstance(command, (app_commands.Command, app_commands.Group)):
            return None

        translations = self._translations[locale]['commands'].get(
            command.qualified_name.replace(' ', '_')
        )
        if not translations:
            return None

        match context.location:
            case TranslationContextLocation.command_name | TranslationContextLocation.group_name:
                return translations.get('name')
            case TranslationContextLocation.command_description | TranslationContextLocation.group_description:
                return translations.get('description')
            case TranslationContextLocation.parameter_name:
                return translations.get('options', {}).get(param.name, {}).get('name')
            case TranslationContextLocation.parameter_description:
                return translations.get('options', {}).get(param.name, {}).get('description')
            case TranslationContextLocation.choice_name:
                if not param:
                    return None
                return translations.get('options', {}).get(param.name, {}).get('choices', {}).get(choice.value)

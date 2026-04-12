from isocodes import countries, currencies, languages

__all__ = ["currencies"]


def get_country(country_code: str):
    country_code = country_code.upper()
    if country := countries.get(alpha_2=country_code) or countries.get(alpha_3=country_code):
        return country

    return None


def get_language(language_code: str):
    language_code = language_code.lower()
    if language := languages.get(alpha_2=language_code) or languages.get(alpha_3=language_code):
        return language

    return None

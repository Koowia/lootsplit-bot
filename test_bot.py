import json
import bot
from bot import parse_amount, plural, clean_nick, format_details


def test_parse_amount_russian_m():
    assert parse_amount('24м') == 24000000


def test_parse_amount_latin_m():
    assert parse_amount('24m') == 24000000


def test_parse_amount_plain_number():
    assert parse_amount('15000000') == 15000000


def test_parse_amount_float():
    assert parse_amount('25.5m') == 25500000


def test_plural_one():
    assert plural(1, ('игрок', 'игрока', 'игроков')) == 'игрок'


def test_plural_two_four():
    assert plural(4, ('игрок', 'игрока', 'игроков')) == 'игрока'


def test_plural_five():
    assert plural(5, ('игрок', 'игрока', 'игроков')) == 'игроков'


def test_plural_teens():
    assert plural(11, ('игрок', 'игрока', 'игроков')) == 'игроков'
    assert plural(14, ('игрок', 'игрока', 'игроков')) == 'игроков'


def test_clean_nick_brackets():
    assert clean_nick('[🔥] Kowin') == 'Kowin'


def test_clean_nick_exclamation():
    assert clean_nick('! iiO') == 'iiO'


def test_clean_nick_complex():
    assert clean_nick('![🔥]Isally') == 'Isally'


def test_format_details_split():
    details = json.dumps({'amount': 24000000, 'per_person': 12000000, 'nicks': ['Kowin', 'iiO']}, ensure_ascii=False)
    result = format_details('split', details)
    assert '24,000,000' in result
    assert 'Kowin' in result


def test_format_details_pay():
    details = json.dumps({'nick': 'iiO', 'amount': 35613892})
    result = format_details('pay', details)
    assert 'Выплата iiO' in result
    assert '35,613,892' in result


def test_format_details_unknown_type():
    assert format_details('undo', 'отменена операция #5') == 'отменена операция #5'


def test_player_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', str(tmp_path / 'test.db'))
    bot.db_init()
    bot.db_run('INSERT INTO players (nick, balance, last_pushed) VALUES (?, ?, ?)', ('Kowin', 100, 100))
    assert bot.get_player('kowin')[1] == 100
    assert bot.get_player('KOWIN')[1] == 100


def test_add_log(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', str(tmp_path / 'test.db'))
    bot.db_init()
    bot.add_log('Kowin', 'split', '{"test": 1}')
    rows = bot.db_all('SELECT author, op_type FROM operations')
    assert rows[0] == ('Kowin', 'split')
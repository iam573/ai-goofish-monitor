from conftest import add_quotes

from app_monitor.quotes import latest_entries
from app_monitor.rules import Rules, search_targets


def test_existing_hardware_names_are_merged_preserving_manual_quote_and_history(database):
    add_quotes(database, [("15 Plus", "128", "小花", 3000), ("15 Plus A3096", "128", "小花", 3100)])
    with database.connect() as connection:
        alias_id = connection.execute("SELECT id FROM quote_entries WHERE model='15 Plus A3096'").fetchone()[0]
        connection.execute("UPDATE quote_entries SET source='manual' WHERE id=?", (alias_id,))
        connection.execute("""INSERT INTO observed_items(item_id,keyword,title,price,item_url,observed_at,model)
                              VALUES('item','iPhone 15 Plus A3096','iPhone 15 Plus',2500,'https://example.test/item','2026-10-23','15 Plus A3096')""")
        connection.execute("""INSERT INTO deals(item_id,model,storage,grade,quote_category,quote_entry_id,
                              quote_price,item_price,expected_margin,threshold_price,reason,matched_at)
                              VALUES('item','15 Plus A3096','128','小花','100025',?,3100,2500,600,3100,'命中','2026-10-23')""", (alias_id,))
        connection.executemany("""INSERT INTO search_runs(model,keyword,started_at,ok)
                                  VALUES(?,?,'2026-10-23',1)""",
                               [("15 Plus", "iPhone 15 Plus"), ("15 Plus A3096", "iPhone 15 Plus A3096")])
    database.state_set('rules', {'disabled_models': ['15 Plus A3096|128', '@series:16']})

    database.initialize()
    database.initialize()  # 重启后重复执行也不丢记录。

    entries = latest_entries(database, '100025')
    assert len(entries) == 1
    assert entries[0]['model'] == '15 Plus' and entries[0]['price'] == 3100 and entries[0]['source'] == 'manual'
    assert [target['keyword'] for target in search_targets(database, Rules())] == ['iPhone 15 Plus']
    with database.connect() as connection:
        deal = connection.execute('SELECT model,quote_entry_id FROM deals').fetchone()
        assert deal['model'] == '15 Plus' and deal['quote_entry_id'] == entries[0]['id']
        runs = connection.execute('SELECT model,keyword FROM search_runs').fetchall()
        assert len(runs) == 2 and all(tuple(row) == ('15 Plus', 'iPhone 15 Plus') for row in runs)
        assert tuple(connection.execute('SELECT model,keyword FROM observed_items').fetchone()) == ('15 Plus', 'iPhone 15 Plus')
    assert database.state_get('rules')['disabled_models'] == ['15 Plus|128', '@series:16']

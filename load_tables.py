import os
import clickhouse_connect

# Get environment variables
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD")
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_SECURE = os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"
CLICKHOUSE_VERIFY = os.getenv("CLICKHOUSE_VERIFY", "true").lower() == "true"

# Connect to ClickHouse
client = clickhouse_connect.get_client(
    host=CLICKHOUSE_HOST,
    user=CLICKHOUSE_USER,
    password=CLICKHOUSE_PASSWORD,
    database=CLICKHOUSE_DATABASE,
    port=CLICKHOUSE_PORT,
    secure=CLICKHOUSE_SECURE,
    verify=CLICKHOUSE_VERIFY,
)

# Table creation and insertion logic
create_table_sql = """
CREATE TABLE IF NOT EXISTS {table_name} (
    event_id UUID,
    user_id UInt32,
    event_type String,
    event_timestamp DateTime,
    event_value Float64
) ENGINE = MergeTree()
ORDER BY (event_timestamp, event_id)
"""

insert_rows_sql = """
INSERT INTO {table_name} (event_id, user_id, event_type, event_timestamp, event_value)
SELECT
    generateUUIDv4(),
    rand() % 1000,
    arrayElement(['click', 'view', 'purchase', 'signup'], rand() % 4 + 1),
    now() - interval rand() % 2592000 second,
    randUniform(0, 1000)
FROM numbers(10)
"""

# Create tables and insert data
for i in range(1, 10_001):
    table_name = f"events_{i}"
    client.command(create_table_sql.format(table_name=table_name))
    client.command(insert_rows_sql.format(table_name=table_name))

    if i % 500 == 0:
        print(f"{i} tables created and populated.")

print("Finished creating and populating 10,000 tables.")

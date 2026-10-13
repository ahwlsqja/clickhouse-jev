CREATE DATABASE IF NOT EXISTS jev_demo;

CREATE OR REPLACE TABLE jev_demo.items
(
    id UInt8,
    item String,
    color String,
    temperature_c Int16
)
ENGINE = Memory;

INSERT INTO jev_demo.items VALUES
    (1, 'red apple', 'red', 12),
    (2, 'yellow banana', 'yellow', 32),
    (3, 'green pear', 'green', 5);

-- Call each SQL function with synthetic rows. Queries send requests to the configured model endpoint.
SELECT jev_prob(
    toJSONString(map('item', 'red apple')),
    'Is the item red?'
) AS probability;

SELECT jev_choice(
    toJSONString(map('item', 'yellow banana')),
    'Choose the item color.',
    toJSONString(['red', 'yellow', 'green'])
) AS color;

SELECT jev_score_norm(
    toJSONString(map('description', 'a small budget hotel')),
    'Rate the level of luxury.',
    toJSONString(['budget', 'mid-range', 'premium', 'luxury'])
) AS luxury;

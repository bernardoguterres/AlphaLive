# Known Limitations

## Adding a new strategy requires manual indicator registration

`alphalive/strategy/indicators.py` computes only the indicators a given
strategy needs, via `_STRATEGY_INDICATOR_DISPATCH` (a `strategy_name ->
_indicators_*` function map). Adding a new strategy means writing a new
`_indicators_*` function and adding it to that dispatch dict by hand -
there's no equivalent of AlphaLab's `required_columns()` pattern, where a
strategy declares what it needs and indicator computation is driven off
that declaration automatically.

Not a bug - the dispatch table works correctly today and strategies are
added rarely - but if AlphaLive gains many more strategies, this manual
step is worth revisiting.

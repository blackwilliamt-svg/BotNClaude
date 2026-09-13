"""Claude as the second-stage trade check. Three call sites, all Sonnet 5,
all with the web_search server tool enabled for real-time market/news context,
all constrained to a JSON schema so the caller never has to free-text-parse a
safety-relevant decision.

Every call's raw response, cost, and parsed decision are logged to the
`decisions` table by the caller (engine.py) — this module only makes the call
and returns a structured result.
"""

import json
import logging
import time

import anthropic

logger = logging.getLogger("bot.claude_gate")

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}

# Sonnet 5 pricing: $2/$10 per 1M input/output tokens, per Anthropic's published
# rates as of this build. Web search itself is billed per use; $0.01/search is
# a rough placeholder — check your Anthropic invoice and adjust if it drifts.
PRICE_PER_INPUT_TOKEN = 2.00 / 1_000_000
PRICE_PER_OUTPUT_TOKEN = 10.00 / 1_000_000
PRICE_PER_WEB_SEARCH = 0.01


class ClaudeGateError(Exception):
    pass


def _estimate_cost(response):
    usage = response.usage
    cost = usage.input_tokens * PRICE_PER_INPUT_TOKEN + usage.output_tokens * PRICE_PER_OUTPUT_TOKEN
    search_uses = sum(1 for b in response.content if getattr(b, "type", None) == "web_search_tool_result")
    cost += search_uses * PRICE_PER_WEB_SEARCH
    return cost


def _extract_json_text(response):
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ClaudeGateError(f"No text block in Claude response (stop_reason={response.stop_reason})")


ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "leverage_recommendation": {"type": "integer", "minimum": 0, "maximum": 50},
        "rationale": {"type": "string"},
    },
    "required": ["approve", "confidence", "leverage_recommendation", "rationale"],
    "additionalProperties": False,
}

CHECKIN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["hold", "tighten_stop", "reduce", "close", "adjust_leverage"]},
        "new_stop_price": {"type": ["number", "null"]},
        "new_size_fraction": {"type": ["number", "null"], "description": "fraction of current size to keep, if reducing"},
        "new_leverage": {"type": ["integer", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "new_stop_price", "new_size_fraction", "new_leverage", "reasoning"],
    "additionalProperties": False,
}

SETTINGS_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "proposed_changes": {"type": "object", "description": "dotted-path config keys to new values"},
        "rationale": {"type": "string"},
    },
    "required": ["proposed_changes", "rationale"],
    "additionalProperties": False,
}


class ClaudeGate:
    def __init__(self, secrets_store, config):
        self.secrets = secrets_store
        self.cfg = config

    def _client(self):
        key = self.secrets.anthropic_api_key()
        if not key:
            raise ClaudeGateError("Anthropic API key not configured in settings")
        return anthropic.Anthropic(api_key=key)

    def _call(self, model, system, user_content, schema, max_tokens=1500):
        client = self._client()
        t0 = time.time()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[WEB_SEARCH_TOOL],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user_content}],
        )
        elapsed = time.time() - t0
        cost = _estimate_cost(response)
        raw_text = _extract_json_text(response)
        parsed = json.loads(raw_text)
        logger.info("Claude call model=%s elapsed=%.1fs cost=$%.4f stop=%s",
                   model, elapsed, cost, response.stop_reason)
        return parsed, response, cost

    # ---- entry check ----------------------------------------------------
    def entry_check(self, candidate, pair_display, account_context):
        model = self.cfg.get("claude.entry_model", "claude-sonnet-5")
        system = (
            "You are a risk-averse second-stage reviewer for a leveraged crypto margin "
            "trading bot. You will be shown one candidate trade that already passed a "
            "technical (RSI/trend/volume) filter and a cost-of-execution filter. Use "
            "web search to check current market conditions and news/sentiment for the "
            "asset. Decide whether to approve the trade, a confidence score (0-100) for "
            "how strongly the technicals, trend, volume, and news/sentiment align, and a "
            "recommended leverage. Recommend LOW or ZERO leverage whenever conditions are "
            "uncertain, conflicting, or major news is pending — leverage amplifies losses "
            "as much as gains. Be concise."
        )
        user_content = (
            f"Candidate trade: {pair_display} {candidate['side'].upper()}\n"
            f"Entry price: {candidate['entry_price']}\n"
            f"Signal: {candidate['reasoning_text']}\n"
            f"Account equity: ${account_context.get('equity', 0):.2f}\n"
            f"Current open positions: {account_context.get('open_position_count', 0)}\n"
            f"Current drawdown from peak: {account_context.get('drawdown_pct', 0):.1f}%\n\n"
            "Search for any current news or market conditions relevant to this asset in "
            "the next few hours to days, then return your decision."
        )
        parsed, response, cost = self._call(model, system, user_content, ENTRY_SCHEMA)
        return parsed, response, cost

    # ---- in-trade check-in ------------------------------------------------
    def checkin_check(self, trade, current_price, unrealized_pnl_pct, pair_display):
        model = self.cfg.get("claude.checkin_model", "claude-sonnet-5")
        system = (
            "You are monitoring an already-open leveraged crypto margin position for a "
            "trading bot. Use web search to check for any new price-moving news since "
            "entry. Recommend one action: hold, tighten_stop, reduce, close, or "
            "adjust_leverage. Bias toward protecting capital over letting a position run "
            "when the original thesis looks weaker than at entry. Be concise."
        )
        user_content = (
            f"Open position: {pair_display} {trade['side'].upper()}\n"
            f"Entry price: {trade['entry_price']}, current price: {current_price}\n"
            f"Leverage: {trade.get('leverage')}x, stop: {trade['stop_price']}, "
            f"est. liquidation: {trade.get('liquidation_price_est')}\n"
            f"Unrealized P&L: {unrealized_pnl_pct:.2f}%\n"
            f"Original reasoning: {trade.get('reasoning_snapshot')}\n\n"
            "Search for relevant news since this position was opened, then recommend "
            "one action. If recommending tighten_stop, give new_stop_price. If reduce, "
            "give new_size_fraction (0-1, fraction of current size to KEEP). If "
            "adjust_leverage, give new_leverage. Set fields you're not using to null."
        )
        parsed, response, cost = self._call(model, system, user_content, CHECKIN_SCHEMA)
        return parsed, response, cost

    # ---- daily settings review ---------------------------------------------
    def settings_review(self, trade_stats_summary, current_config_snapshot):
        model = self.cfg.get("claude.settings_review_model", "claude-sonnet-5")
        system = (
            "You periodically review this trading bot's performance and propose "
            "parameter adjustments. You may NOT change hard-coded safety limits (risk "
            "cap, exposure caps, drawdown breaker, max leverage utilization) — only the "
            "tunable parameters shown. Focus especially on recent losing trades: what "
            "pattern, if any, explains them, and what specific parameter change would "
            "address it. Propose changes only when you have real evidence from the data; "
            "propose no changes if performance looks fine. Be concise and specific."
        )
        user_content = (
            f"Aggregated trade history and recent losses:\n{json.dumps(trade_stats_summary, indent=2)}\n\n"
            f"Current tunable settings:\n{json.dumps(current_config_snapshot, indent=2)}\n\n"
            "Propose config changes as dotted-path keys (e.g. 'risk.atr_stop_mult') "
            "mapped to new values, with your rationale. Return an empty object for "
            "proposed_changes if no change is warranted."
        )
        parsed, response, cost = self._call(model, system, user_content, SETTINGS_REVIEW_SCHEMA, max_tokens=3000)
        return parsed, response, cost

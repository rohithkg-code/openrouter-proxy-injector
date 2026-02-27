# Changelog

## [Unreleased - available on :latest, :dev tags for docker image]
### Changed
- Refactored key rotation logic from random selection to a quota-aware prioritization strategy.
- Updated `OPENROUTER_KEYS` configuration to support optional daily limits per key (e.g., `KEY:LIMIT`).
- Improved `/key-status` endpoint to provide detailed usage statistics, remaining quotas, and last usage timestamps.
- Optimized `proxy_request` to eliminate redundant key selection and prevent double-counting of requests.

### Added
- Proactive RPM (Requests Per Minute) throttling to ensure keys do not exceed the 20 RPM limit.
- Daily quota tracking and automatic reset logic at UTC midnight.
- Intelligent key selection that prioritizes keys with the highest percentage of remaining daily quota.
- Automatic synchronization of internal usage counters when receiving rate-limit errors from the OpenRouter API.
- Enhanced logging for key selection decisions and quota usage monitoring.

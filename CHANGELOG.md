# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.6.0] - 2024-03-27
Added
Added retry mechanism for APIs getting used inside extension library if Fynd Platform server is down.

---
## [v0.5.2] - 2022-12-31
### Added
- Added support to auto install extensions feature. Now selected auto install extensions will be installed for company. It will generate access token for offline mode and register webhook subscribers for company it is getting auto installed.

### Changed
- Fixed TTL not getting set for `online` mode access token set for extension users. It caused redis to fill-in with non-expiring keys since all users who launches extension, a new session key is generated and stored without TTL due to this bug. 

- With introduction of auto install feature, any handling done on extension installation or first launch event on `auth` callback should be done here as well. Since auto install event will install extension in background on company creation. And `auth` callback is only triggered when extension is launched.
---
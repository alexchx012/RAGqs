## ADDED Requirements

### Requirement: Authentication Providers And Defaults
The system SHALL support disabled local authentication, dev-header authentication, and
reverse-proxy header authentication.

#### Scenario: Local auth disabled uses default admin
- **WHEN** `AUTH_ENABLED=false`
- **THEN** the system SHALL authenticate requests as the configured default local user with
  default roles and spaces, preserving local development compatibility

#### Scenario: Header auth resolves user roles and spaces
- **WHEN** authentication is enabled with `dev_header` or `reverse_proxy`
- **THEN** the system SHALL read configured user, role, and space headers or configured
  dev-user mappings and SHALL reject missing or unknown dev users

### Requirement: Role And Space Authorization
The system SHALL enforce permission and knowledge-space access checks on chat, session,
audit, upload, knowledge-space, document, indexing-job, and metrics routes.

#### Scenario: Permission is required per route family
- **WHEN** a user lacks the permission required by a route
- **THEN** the system SHALL return HTTP 403 before executing the protected operation

#### Scenario: Session and audit access are space filtered
- **WHEN** an authenticated user is limited to specific knowledge spaces
- **THEN** session summaries, session detail/clear, and retrieval audits SHALL be limited
  to sessions or records whose space metadata is accessible to that user

### Requirement: Upload Security Boundary
The system SHALL protect the upload path before indexing.

#### Scenario: Unsafe filenames and paths are rejected or normalized
- **WHEN** an upload filename contains path separators, unsafe characters, empty names, or
  parent traversal attempts
- **THEN** the system SHALL sanitize the basename and reject any target path that escapes
  the upload directory

#### Scenario: Text upload constraints are enforced
- **WHEN** uploaded content exceeds configured size, has an unsupported extension, is not
  valid UTF-8, or matches configured prompt-injection patterns
- **THEN** the system SHALL reject the upload with an HTTP 400 error before writing and
  indexing it

### Requirement: CORS And Runtime Request Controls
The system SHALL expose configurable CORS and optional process-local request concurrency
and timeout controls.

#### Scenario: Credentialed wildcard CORS is rejected
- **WHEN** CORS credentials are enabled with wildcard origins
- **THEN** configuration validation SHALL reject the setting as unsafe

#### Scenario: Runtime controls reject or time out overloaded requests
- **WHEN** runtime controls are enabled and a request cannot acquire a process-local slot
  before queue timeout or exceeds request timeout
- **THEN** the system SHALL return the standard envelope with HTTP 429 or HTTP 504 while
  excluding configured paths such as health/static routes

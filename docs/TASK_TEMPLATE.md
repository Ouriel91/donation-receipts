# TASK TEMPLATE

## Task Name

Short descriptive task name.

---

## Goal

Describe the exact goal of the task.

Keep the scope narrow and focused.

---

## In Scope

List the main intended scope for this task.

Possible examples:

- Add receipt detection logic
- Add unit tests
- Add harness sample

---

## Out of Scope

Explicitly list what is NOT allowed.

Possible examples:

- No Gmail integration
- No refactoring unrelated modules
- No new frameworks
- No database changes

---

## Primary Files Expected To Change

List the main files expected to change during this task.

Possible examples:

- src/receipt_detector.py
- tests/test_receipt_detector.py
- harness/emails/

Minor supporting changes outside this list should remain minimal and justified.

---

## Expected Inputs

Describe the expected inputs.

Possible examples:

- Email subject
- Email body
- Attachment metadata

---

## Expected Outputs

Describe the expected outputs.

Possible examples:

- Boolean donation detection result
- Detection reason
- Saved file path

---

## Testing Requirements

Describe the required tests.

Possible examples:

- Detect donation email
- Ignore normal email
- Handle missing attachment
- Handle duplicate filenames

---

## Harness Requirements

Describe required harness coverage.

Possible examples:

- Add at least one positive sample
- Add at least one negative sample

---

## Definition of Done

The task is complete only if:

- Implementation works
- Tests pass
- Harness works
- No unrelated files changed
- No secrets added
- No overengineering introduced

---

## Additional Notes

Optional implementation notes and constraints.
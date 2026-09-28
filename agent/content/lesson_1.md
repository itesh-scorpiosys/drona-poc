# Lesson 1: Your first call to the Messages API

## Objectives
- Name the required parts of a Messages API request.
- Explain the user / assistant turn structure.
- Read a response: content blocks, stop_reason, usage.

## Key points
1. A request needs model, max_tokens and messages.
2. messages is a list of turns with role user or assistant, alternating,
starting with user.
3. Instructions for the whole conversation go in the top-level system
parameter, not as a message.
4. The response content is a list of blocks; text lives in blocks of
type text.
5. stop_reason tells you why generation ended: end_turn, max_tokens,
stop_sequence or tool_use.
6. usage reports input_tokens and output_tokens, which drive cost.

## Check questions
- What happens if max_tokens is missing? (The request is rejected.)
- Where does a system prompt go? (Top-level system parameter.)
- stop_reason is max_tokens. What does that mean? (The answer was cut off.)

## Common misconceptions
- Putting role: system inside messages.
- Assuming the response is a plain string rather than content blocks.

## Practice task (Cowork)
Write a request that asks for a three-line summary with a system prompt
setting a formal tone, and explain each field.

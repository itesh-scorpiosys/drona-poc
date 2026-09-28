# Lesson 2: Streaming responses

## Objectives
- Explain why streaming improves perceived speed.
- Name the streaming events in order.
- Assemble text from a stream correctly.

## Key points
1. Set stream to true to receive server-sent events instead of one reply.
2. Event order: message_start, content_block_start, content_block_delta
(repeated), content_block_stop, message_delta, message_stop.
3. Text arrives in content_block_delta events as text_delta pieces;
join them in order.
4. message_delta carries the final stop_reason and output token usage.
5. The SDKs provide stream helpers that assemble the final message.

## Check questions
- Where does the final stop_reason arrive? (message_delta.)
- How do you rebuild the full text? (Join text_delta pieces in order.)
- Why stream in a chat UI? (The user sees words immediately.)

## Common misconceptions
- Expecting usage totals in the first event.
- Treating each delta as a complete sentence.

## Practice task (Cowork)
Describe, event by event, what a client receives when streaming a
two-sentence answer.

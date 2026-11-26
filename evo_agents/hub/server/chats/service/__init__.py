"""The service layer of group chats: who may read and write a chat (``access``), what a chat shows (``views``,
``summaries``), a person's message and the work its mentions hand (``messages``, ``runs``), the agent of a run in its
chats (``agents``, ``pulls``, ``author``, ``read``) and the rest of a chat's life. It reads and writes through the
repository and raises HTTPException on a refusal."""

"""The repository layer of group chats: every SQL statement on the chat tables and on the hub's tables they read, with
SQLAlchemy Core on ``evo_agents.hub.tables``; ``store`` writes a chat's messages, members, reactions and events. Its
functions take a connection the caller's transaction holds; they check no grant and decide nothing."""

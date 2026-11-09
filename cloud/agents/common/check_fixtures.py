"""Scripted model support for Graph regression suites; never used by services."""

import json

from langchain_core.runnables import RunnableLambda


class JsonReplyModel:
    """Bind JSON replies without changing a fake model's persona responses."""

    def bind(self, **kwargs):
        assert kwargs == {"extra_body": {"response_format": {"type": "json_object"}}}

        async def invoke(messages):
            response = await self.ainvoke(messages)
            if (response.tool_calls or not response.content
                    or response.content.lstrip().startswith("{")):
                return response
            return response.model_copy(update={"content": json.dumps(
                {"kind": "final", "text": response.content}, ensure_ascii=False)})

        return RunnableLambda(invoke)

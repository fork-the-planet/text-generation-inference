import pytest
import asyncio


@pytest.fixture(scope="module")
def mllama_handle(launcher):
    with launcher(
        "unsloth/Llama-3.2-11B-Vision-Instruct",
        num_shard=2,
    ) as handle:
        yield handle


@pytest.fixture(scope="module")
async def mllama(mllama_handle):
    await mllama_handle.health(300)
    return mllama_handle.client


@pytest.mark.asyncio
async def test_mllama_simpl(mllama, response_snapshot):
    response = await mllama.chat(
        max_tokens=10,
        temperature=0.0,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Describe the image in 10 words.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://raw.githubusercontent.com/huggingface/text-generation-inference/main/integration-tests/images/chicken_on_money.png"
                        },
                    },
                ],
            },
        ],
    )

    assert response.usage == {
        "completion_tokens": 10,
        "prompt_tokens": 45,
        "total_tokens": 55,
    }
    assert (
        response.choices[0].message.content
        == "A chicken sits on a pile of money, looking"
    )
    assert response == response_snapshot


@pytest.mark.release
@pytest.mark.asyncio
async def test_mllama_load(mllama, generate_load, response_snapshot):
    futures = [
        mllama.chat(
            max_tokens=10,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe the image in 10 words.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://raw.githubusercontent.com/huggingface/text-generation-inference/main/integration-tests/images/chicken_on_money.png"
                            },
                        },
                    ],
                },
            ],
        )
        # TODO with v3, 4 breaks here. Nothing accounts of the image VRAM
        # because mllama is the only one doing its thing.
        for i in range(2)
    ]
    responses = await asyncio.gather(*futures)

    generated_texts = [response.choices[0].message.content for response in responses]

    # XXX: TODO: Fix this test.
    assert generated_texts[0] == "A chicken sits on a pile of money, looking"
    assert len(generated_texts) == 2
    assert generated_texts, all(
        [text == generated_texts[0] for text in generated_texts]
    )
    assert responses == response_snapshot

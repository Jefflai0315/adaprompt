import torch
import torch.nn as nn
from functools import partial
import clip
from einops import rearrange, repeat
from transformers import CLIPTokenizer, CLIPTextModel
import kornia

from ldm.modules.x_transformer import Encoder, TransformerWrapper  # TODO: can we directly rely on lucidrains code and simply add this as a reuirement? --> test

def _expand_mask(mask, dtype, tgt_len = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)

def _build_causal_attention_mask(bsz, seq_len, dtype):
        # lazily create causal attention mask, with full attention between the vision tokens
        # pytorch uses additive attention mask; fill with -inf
        mask = torch.empty(bsz, seq_len, seq_len, dtype=dtype)
        mask.fill_(torch.tensor(torch.finfo(dtype).min))
        mask.triu_(1)  # zero out the lower diagonal
        mask = mask.unsqueeze(1)  # expand mask
        return mask

class AbstractEncoder(nn.Module):
    def __init__(self):
        super().__init__()

    def encode(self, *args, **kwargs):
        raise NotImplementedError



class ClassEmbedder(nn.Module):
    def __init__(self, embed_dim, n_classes=1000, key='class'):
        super().__init__()
        self.key = key
        self.embedding = nn.Embedding(n_classes, embed_dim)

    def forward(self, batch, key=None):
        if key is None:
            key = self.key
        # this is for use in crossattn
        c = batch[key][:, None]
        c = self.embedding(c)
        return c


class TransformerEmbedder(AbstractEncoder):
    """Some transformer encoder layers"""
    def __init__(self, n_embed, n_layer, vocab_size, max_seq_len=77, device="cuda"):
        super().__init__()
        self.device = device
        self.transformer = TransformerWrapper(num_tokens=vocab_size, max_seq_len=max_seq_len,
                                              attn_layers=Encoder(dim=n_embed, depth=n_layer))

    def forward(self, tokens):
        tokens = tokens.to(self.device)  # meh
        z = self.transformer(tokens, return_embeddings=True)
        return z

    def encode(self, x):
        return self(x)


class BERTTokenizer(AbstractEncoder):
    """ Uses a pretrained BERT tokenizer by huggingface. Vocab size: 30522 (?)"""
    def __init__(self, device="cuda", vq_interface=True, max_length=77):
        super().__init__()
        from transformers import BertTokenizerFast  # TODO: add to reuquirements
        self.tokenizer = BertTokenizerFast.from_pretrained("bert-base-uncased")
        self.device = device
        self.vq_interface = vq_interface
        self.max_length = max_length

    def forward(self, text):
        batch_encoding = self.tokenizer(text, truncation=True, max_length=self.max_length, return_length=True,
                                        return_overflowing_tokens=False, padding="max_length", return_tensors="pt")
        tokens = batch_encoding["input_ids"].to(self.device)
        return tokens

    @torch.no_grad()
    def encode(self, text):
        tokens = self(text)
        if not self.vq_interface:
            return tokens
        return None, None, [None, None, tokens]

    def decode(self, text):
        return text


class BERTEmbedder(AbstractEncoder):
    """Uses the BERT tokenizr model and add some transformer encoder layers"""
    def __init__(self, n_embed, n_layer, vocab_size=30522, max_seq_len=77,
                 device="cuda",use_tokenizer=True, embedding_dropout=0.0):
        super().__init__()
        self.use_tknz_fn = use_tokenizer
        if self.use_tknz_fn:
            self.tknz_fn = BERTTokenizer(vq_interface=False, max_length=max_seq_len)
        self.device = device
        self.transformer = TransformerWrapper(num_tokens=vocab_size, max_seq_len=max_seq_len,
                                              attn_layers=Encoder(dim=n_embed, depth=n_layer),
                                              emb_dropout=embedding_dropout)

    def forward(self, text, embedding_manager=None):
        if self.use_tknz_fn:
            tokens = self.tknz_fn(text)#.to(self.device)
        else:
            tokens = text
        z = self.transformer(tokens, return_embeddings=True, embedding_manager=embedding_manager)
        return z

    def encode(self, text, **kwargs):
        # output of length 77
        return self(text, **kwargs)

class SpatialRescaler(nn.Module):
    def __init__(self,
                 n_stages=1,
                 method='bilinear',
                 multiplier=0.5,
                 in_channels=3,
                 out_channels=None,
                 bias=False):
        super().__init__()
        self.n_stages = n_stages
        assert self.n_stages >= 0
        assert method in ['nearest','linear','bilinear','trilinear','bicubic','area']
        self.multiplier = multiplier
        self.interpolator = partial(torch.nn.functional.interpolate, mode=method)
        self.remap_output = out_channels is not None
        if self.remap_output:
            print(f'Spatial Rescaler mapping from {in_channels} to {out_channels} channels after resizing.')
            self.channel_mapper = nn.Conv2d(in_channels,out_channels,1,bias=bias)

    def forward(self,x):
        for stage in range(self.n_stages):
            x = self.interpolator(x, scale_factor=self.multiplier)


        if self.remap_output:
            x = self.channel_mapper(x)
        return x

    def encode(self, x):
        return self(x)

class FrozenCLIPEmbedder(AbstractEncoder):
    """Uses the CLIP transformer encoder for text (from Hugging Face)"""
    def __init__(self, version="openai/clip-vit-large-patch14", device="cuda", max_length=77,
                 last_layer_skip_weight=0.0):
        super().__init__()
        self.tokenizer = CLIPTokenizer.from_pretrained(version)
        self.transformer = CLIPTextModel.from_pretrained(version)
        self.device = device
        self.max_length = max_length
        self.set_last_layer_skip_weight(last_layer_skip_weight)

        def embedding_forward(
                self,
                input_ids = None,
                position_ids = None,
                inputs_embeds = None,
                embedding_manager = None,
            ) -> torch.Tensor:

                seq_length = input_ids.shape[-1] if input_ids is not None else inputs_embeds.shape[-2]

                if position_ids is None:
                    position_ids = self.position_ids[:, :seq_length]

                if inputs_embeds is None:
                    inputs_embeds = self.token_embedding(input_ids)

                # The two lines of embedding_manager are newly added. Other code pieces 
                # are the same as the original CLIPTextEmbeddings.forward().
                # EmbeddingManager::forward() patches inputs_embeds by replacing CLIP embeddings of placeholder
                # tokens with the learned embeddings. 
                if embedding_manager is not None:
                    inputs_embeds = embedding_manager(input_ids, inputs_embeds)

                position_embeddings = self.position_embedding(position_ids)
                embeddings = inputs_embeds + position_embeddings
                
                return embeddings      

        # embeddings: CLIPTextEmbeddings
        # embeddings.forward = embedding_forward.__get__(obj)
        # when calling embeddings(arguments), it will call embedding_forward(obj, arguments) instead.
        # Therefore, the method is intercepted without modifying the implicit object "embeddings".
        self.transformer.text_model.embeddings.forward = embedding_forward.__get__(self.transformer.text_model.embeddings)

        def encoder_forward(
            self,
            inputs_embeds,
            attention_mask = None,
            causal_attention_mask = None,
            output_attentions = None,       # default: None
            output_hidden_states = None,    # default: None
            return_dict = None,
            last_layer_skip_weight = 0.0,
        ):
            output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
            output_hidden_states = (
                output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
            )
            return_dict = return_dict if return_dict is not None else self.config.use_return_dict

            encoder_states = () if output_hidden_states else None
            all_attentions = () if output_attentions else None

            hidden_states = inputs_embeds
            penultimate_hidden_states = None

            for idx, encoder_layer in enumerate(self.layers):
                if output_hidden_states:
                    encoder_states = encoder_states + (hidden_states,)

                layer_outputs = encoder_layer(
                    hidden_states,
                    attention_mask,
                    causal_attention_mask,
                    output_attentions=output_attentions,
                )

                # Only keep the last layer's hidden states
                hidden_states = layer_outputs[0]

                # output_attentions: None
                if output_attentions:
                    all_attentions = all_attentions + (layer_outputs[1],)

                # NovalAI modification: skip the last layer to make the 
                # text embeddings more accurate, at the cost of slight performance reduction.
                if last_layer_skip_weight > 0 and idx == len(self.layers) - 2:
                    penultimate_hidden_states = hidden_states

            # output_hidden_states: None
            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)

            # Only return the last layer's and the second last layer's hidden states
            return (penultimate_hidden_states, hidden_states)


        # encoder: CLIPEncoder
        # encoder.forward = encoder_forward.__get__(obj)
        # when calling encoder(arguments), it will call encoder_forward(obj, arguments) instead.
        # Therefore, the method is intercepted without modifying the implicit object "encoder".
        # It's implemented differently from the huggingface transformers, in that only
        # hidden_states is returned, in contrast to a tuple of (hidden_states, encoder_states, all_attentions).
        # Therefore, text_encoder_forward() below is also implemented differently (simpler than
        # the CLIPTextTransformer.forward() in huggingface transformers).
        self.transformer.text_model.encoder.forward = encoder_forward.__get__(self.transformer.text_model.encoder)


        def text_encoder_forward(
            self,
            input_ids = None,
            attention_mask = None,
            position_ids = None,
            output_attentions = None,
            output_hidden_states = None,
            return_dict = None,
            embedding_manager = None,
        ):
            output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
            output_hidden_states = (
                output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
            )
            return_dict = return_dict if return_dict is not None else self.config.use_return_dict

            if input_ids is None:
                raise ValueError("You have to specify either input_ids")

            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])

            hidden_states = self.embeddings(input_ids=input_ids, position_ids=position_ids, embedding_manager=embedding_manager)
            # the batch size could be modified by embedding_manager
            bsz = hidden_states.shape[0]
            seq_len = input_shape[1]
            # CLIP's text model uses causal mask, prepare it here.
            # https://github.com/openai/CLIP/blob/cfcffb90e69f37bf2ff1e988237a0fbe41f33c04/clip/model.py#L324
            causal_attention_mask = _build_causal_attention_mask(bsz, seq_len, hidden_states.dtype).to(
                hidden_states.device
            )

            # expand attention_mask
            if attention_mask is not None:
                # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
                attention_mask = _expand_mask(attention_mask, hidden_states.dtype)

            last_hidden_states = self.encoder(
                inputs_embeds=hidden_states,
                attention_mask=attention_mask,
                causal_attention_mask=causal_attention_mask,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                last_layer_skip_weight=self.last_layer_skip_weight,
            )

            # encoder() returns a tuple of (penultimate_hidden_states, last_hidden_states).
            # If self.last_layer_skip_weight is 0, then penultimate_hidden_states = None.
            penultimate_hidden_states, last_hidden_states = last_hidden_states
            # Note that the original implementation in huggingface transformers
            # returns a tuple of (last_hidden_state, encoder_states, all_attentions).
            # However, the overloading text_encoder_forward() above only returns
            # hidden_states. So it's passed to the final_layer_norm() directly.
            if self.last_layer_skip_weight > 0:
                # According to NovelAI's practice, the penultimate_hidden_states is layernormed
                # with the same parameters as the last_hidden_states.
                # penultimate_hidden_states = self.final_layer_norm(penultimate_hidden_states)
                # penultimate_hidden_states: [32, 77, 768], last_hidden_states: [32, 77, 768]
                last_hidden_states = self.last_layer_skip_weight * penultimate_hidden_states + \
                                    (1 - self.last_layer_skip_weight) * last_hidden_states


            last_hidden_states = self.final_layer_norm(last_hidden_states)
            return last_hidden_states

        # text_model: CLIPTextTransformer
        # text_model.forward = text_encoder_forward.__get__(obj)
        # when calling text_model(arguments), it will call text_encoder_forward(obj, arguments) instead.
        # Therefore, the method is intercepted without modifying the implicit object "text_model".
        self.transformer.text_model.forward = text_encoder_forward.__get__(self.transformer.text_model)

        def transformer_forward(
            self,
            input_ids = None,
            attention_mask = None,
            position_ids = None,
            output_attentions = None,
            output_hidden_states = None,
            return_dict = None,
            embedding_manager = None,
        ):
            # In the original implementation in huggingface, transformer.forward()
            # simply calls text_model.forward(). Here it's the same, except that 
            # we pass the embedding_manager to text_encoder_forward.forward().
            return self.text_model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                embedding_manager = embedding_manager,
            )

        # transformer: CLIPTextModel
        # transformer.forward = transformer_forward.__get__(obj)
        # when calling transformer(arguments), it will call transformer_forward(obj, arguments) instead.
        # Therefore, the method is intercepted without modifying the implicit object "transformer".
        self.transformer.forward = transformer_forward.__get__(self.transformer)

    def set_last_layer_skip_weight(self, weight):
        self.transformer.text_model.last_layer_skip_weight = weight
        print(f"CLIP last_layer_skip_weight = {weight}")
              
    def freeze(self):
        self.transformer = self.transformer.eval()
        for param in self.parameters():
            param.requires_grad = False

    # text: ['an illustration of a dirty z', 'an illustration of the cool z']
    # kwargs: embedding_manager
    def forward(self, text, **kwargs):
        # tokenizer: CLIPTokenizer.
        batch_encoding = self.tokenizer(text, truncation=True, max_length=self.max_length, return_length=True,
                                        return_overflowing_tokens=False, padding="max_length", return_tensors="pt")
        tokens = batch_encoding["input_ids"].to(self.device)      
        # transformer: CLIPTextModel. 
        # transformer.text_model: CLIPTextTransformer. 
        # transformer.text_model.encoder: CLIPEncoder
        # transformer.text_model.embeddings: CLIPTextEmbeddings
        z = self.transformer(input_ids=tokens, **kwargs)

        return z

    def encode(self, text, **kwargs):
        return self(text, **kwargs)


class FrozenCLIPTextEmbedder(nn.Module):
    """
    Uses the CLIP transformer encoder for text.
    """
    def __init__(self, version='ViT-L/14', device="cuda", max_length=77, n_repeat=1, normalize=True):
        super().__init__()
        self.model, _ = clip.load(version, jit=False, device="cpu")
        self.device = device
        self.max_length = max_length
        self.n_repeat = n_repeat
        self.normalize = normalize

    def freeze(self):
        self.model = self.model.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text):
        tokens = clip.tokenize(text).to(self.device)
        z = self.model.encode_text(tokens)
        if self.normalize:
            z = z / torch.linalg.norm(z, dim=1, keepdim=True)
        return z

    def encode(self, text):
        z = self(text)
        if z.ndim==2:
            z = z[:, None, :]
        z = repeat(z, 'b 1 d -> b k d', k=self.n_repeat)
        return z


class FrozenClipImageEmbedder(nn.Module):
    """
        Uses the CLIP image encoder.
        """
    def __init__(
            self,
            model,
            jit=False,
            device='cuda' if torch.cuda.is_available() else 'cpu',
            antialias=False,
        ):
        super().__init__()
        self.model, _ = clip.load(name=model, device=device, jit=jit)

        self.antialias = antialias

        self.register_buffer('mean', torch.Tensor([0.48145466, 0.4578275, 0.40821073]), persistent=False)
        self.register_buffer('std', torch.Tensor([0.26862954, 0.26130258, 0.27577711]), persistent=False)

    def preprocess(self, x):
        # normalize to [0,1]
        x = kornia.geometry.resize(x, (224, 224),
                                   interpolation='bicubic',align_corners=True,
                                   antialias=self.antialias)
        x = (x + 1.) / 2.
        # renormalize according to clip
        x = kornia.enhance.normalize(x, self.mean, self.std)
        return x

    def forward(self, x):
        # x is assumed to be in range [-1,1]
        return self.model.encode_image(self.preprocess(x))


if __name__ == "__main__":
    from ldm.util import count_params
    model = FrozenCLIPEmbedder()
    count_params(model, verbose=True)
import logging

from attr import Factory
from datamaestro import prepare_dataset
from datamaestro_text.data.conversation.orconvqa import OrConvQADataset

from xpmir.rankers import document_cache, Retriever, Documents
from xpmir.conversation.learning import DatasetConversationEntrySampler
from xpmir.conversation.learning.reformulation import DecontextualizedQueryConverter
from xpmir.conversation.models.cosplade import (
    AsymetricMSEContextualizedRepresentationLoss,
    CoSPLADE,
)
from xpmir.evaluation import EvaluationsCollection, Evaluations
from xpmir.experiments.helpers import LauncherSpecification, NeuralIRExperiment
from xpmir.experiments.ir import IRExperimentHelper, ir_experiment
from xpmir.index.sparse import SparseRetrieverIndexBuilder, SparseRetriever
from xpmir.learning.batchers import PowerAdaptativeBatcher
from xpmir.learning.devices import CudaDevice
from xpmir.learning.learner import Learner, LearnerOutput
from xpmir.letor.trainers.alignment import AlignmentTrainer, MSEAlignmentLoss
from xpmir.neural.splade import MaxAggregation, SpladeTextEncoderV2
from xpmir.papers import configuration
from xpmir.papers.results import PaperResults
from xpmir.text.huggingface.base import HFMaskedLanguageModel, HFModelConfigFromId
from xpmir.text.huggingface import HFTokenizer, HFStringTokenizer, HFListTokenizer, HFTokenizerAdapter
import xpmir.measures as m
from xpmir.papers.helpers.optim import TransformerOptimization

logging.basicConfig(level=logging.INFO)


@configuration
class Launchers:
    learner: LauncherSpecification = Factory(
        lambda: LauncherSpecification(requirements="cpu(cores=1)")
    )
    splade_indexer: LauncherSpecification = Factory(
        lambda: LauncherSpecification(requirements="cpu(cores=1)")
    )


@configuration()
class Configuration(NeuralIRExperiment):
    """Experimental configuration"""

    splade_model_id: str = "naver/splade-cocondenser-ensembledistil"
    """Base HF id for SPLADE models"""

    launchers: Launchers = Factory(Launchers)
    """Launchers"""

    retrieval_topK: int = 1000
    """How many documents to retrieve"""
    
    optimization: TransformerOptimization = Factory(TransformerOptimization)


MEASURES = [
    m.R @ 1000,
    m.AP @ 1000,
    m.nDCG @ 3,
    m.nDCG @ 20,
    m.nDCG @ 1000,
    m.RR @ 10,
]


@ir_experiment()
def run(helper: IRExperimentHelper, cfg: Configuration) -> PaperResults:
    """CoSPLADE training"""

    # --- Get launchers

    device = CudaDevice.C()
    launcher_learner = cfg.launchers.learner.launcher
    launcher_splade_indexer = cfg.launchers.splade_indexer.launcher

    # --- SPLADE (from HuggingFace)

    tokenizer = HFTokenizer.C(model_id=cfg.splade_model_id)
    splade_encoder = SpladeTextEncoderV2.C(
        tokenizer=HFStringTokenizer.C(tokenizer=tokenizer),
        encoder=HFMaskedLanguageModel.from_pretrained_id(cfg.splade_model_id),
        aggregation=MaxAggregation.C(),
    )

   
    # --- Evaluation
    
    tests = EvaluationsCollection(
        cast_2019=Evaluations(
            prepare_dataset("irds.trec-cast.v1.2019"), MEASURES
        ),
        cast_2020=Evaluations(
            prepare_dataset("irds.trec-cast.v1.2020"), MEASURES
        ),
        # TODO: needs to use passages here
        # cast_2021=Evaluations(
        #     prepare_dataset("irds.trec-cast.v2.2021"), MEASURES
        # ),
    )

    @document_cache
    def splade_index(
        documents: Documents
    ):
        return SparseRetrieverIndexBuilder.C(
            batch_size=512,
            batcher=PowerAdaptativeBatcher(),
            encoder=splade_encoder,
            device=device,
            documents=documents,
            ordered_index=False,
            max_docs=0,
        ).submit(launcher=launcher_splade_indexer)

    # --- Evaluate with manually rewritten queries

    splade_gold_encoder = SpladeTextEncoderV2.C(
        tokenizer=HFTokenizerAdapter.C(tokenizer=tokenizer, converter=DecontextualizedQueryConverter.C()),
        encoder=HFMaskedLanguageModel.from_pretrained_id(cfg.splade_model_id),
        aggregation=MaxAggregation.C(),
    )

    def gold_splade_retriever(
        documents: Documents,
    ) -> Retriever.C:
        return SparseRetriever.C(
            index=splade_index()(documents),
            topk=cfg.retrieval_topK,
            batchsize=1,
            encoder=splade_gold_encoder,
        )

    tests.evaluate_retriever(gold_splade_retriever)

    # --- Learn CoSPLADE (1st stage)
    
    orConvQA: OrConvQADataset = prepare_dataset(
        "com.github.prdwb.orconvqa.preprocessed"
    )
    sampler = DatasetConversationEntrySampler.C(dataset=orConvQA.train)

    history_encoder = SpladeTextEncoderV2.C(
        tokenizer=HFStringTokenizer.C(tokenizer=tokenizer),
        encoder=HFMaskedLanguageModel.C(
            config=HFModelConfigFromId.C(model_id=cfg.splade_model_id)
        ),
        aggregation=MaxAggregation(),
    )
    queries_encoder = SpladeTextEncoderV2.C(
        tokenizer=HFListTokenizer.C(tokenizer=tokenizer),
        encoder=HFMaskedLanguageModel.C(
            config=HFModelConfigFromId.C(model_id=cfg.splade_model_id)
        ),
        aggregation=MaxAggregation.C(),
    )
    cosplade = CoSPLADE.C(
        history_size=0, history_encoder=history_encoder, queries_encoder=queries_encoder
    )

    trainer = AlignmentTrainer.C(
        sampler=sampler,
        target_model=splade_gold_encoder,
        losses={
            "mse": MSEAlignmentLoss.C(),
            "amse": AsymetricMSEContextualizedRepresentationLoss.C(),
        },
    )

    learner = Learner.C(
        random=cfg.random,
        model=cosplade,
        max_epochs=100,
        optimizers=cfg.optimization.optimizer,
        trainer=trainer,
        use_fp16=True,
        listeners=[]
    )
    output = learner.submit(launcher=launcher_learner)  # type: LearnerOutput
    helper.tensorboard_service.add(learner, learner.logpath)

    # --- Evaluate on CoSPLADE

    def cosplade_retriever(
        documents: Documents,
    ) -> Retriever.C:
        # Returns a retrieve that uses CoSPLADE (for query encoding)
        # and the built index
        return SparseRetriever.C(
            index=splade_index()(documents),
            topk=cfg.retrieval_topK,
            batchsize=1,
            encoder=cosplade,
        )

    tests.evaluate_retriever(cosplade_retriever, init_tasks=[output.learned_model])

    # --- Return results
    return PaperResults(
        models={"cosplade-RR@10": cosplade},
        evaluations=tests,
        tb_logs={"cosplade-RR@10": learner.logpath},
    )

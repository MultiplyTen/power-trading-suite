"""Serializable model contract, with preprocessing owned by each fitted model."""
import importlib
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge,LogisticRegression

class LinearPlugin:
    name='baseline_ridge';supports_incremental=False
    def __init__(self,alpha=100,classification=False):
        self.alpha=alpha;self.classification=classification;self.meta={}
    def fit(self,X,y,sample_weight=None):
        self.columns=list(X);self.constant=None
        if self.classification and pd.Series(y).nunique()<2:self.constant=float(np.mean(y));return self
        estimator=LogisticRegression(C=self.alpha,max_iter=1000) if self.classification else Ridge(alpha=self.alpha)
        self.pipeline=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),StandardScaler(),estimator)
        kw={} if sample_weight is None else {self.pipeline.steps[-1][0]+'__sample_weight':sample_weight}
        self.pipeline.fit(X,y,**kw);return self
    def predict(self,X):
        self.check_columns(X)
        return np.full(len(X),self.constant) if self.constant is not None else self.pipeline.predict(X)
    def predict_proba(self,X):
        self.check_columns(X)
        p=np.full(len(X),self.constant) if self.constant is not None else self.pipeline.predict_proba(X)[:,1]
        return np.clip(p,.001,.999)
    def check_columns(self,X):
        if list(X)!=self.columns:raise ValueError('模型输入字段/顺序发生变化，请重训')
    def save(self,path):
        self.meta['sklearn_version']=sklearn.__version__;self.meta['features']=self.columns
        Path(path).parent.mkdir(parents=True,exist_ok=True);joblib.dump(self,path)
    @classmethod
    def load(cls,path,cutoff=None):
        m=joblib.load(path)
        if m.meta.get('sklearn_version')!=sklearn.__version__:raise ValueError('模型依赖版本已变化，请重训')
        if cutoff is not None and pd.Timestamp(m.meta['label_end'])>pd.Timestamp(cutoff):raise ValueError('模型训练标签晚于请求截点')
        return m
    def coefficients(self):
        if self.constant is not None:return []
        im,sc,est=[v for _,v in self.pipeline.steps];co=np.ravel(est.coef_)
        rows=[dict(feature=n,standardized_coefficient=float(v),coefficient=float(v/s),impute_value=float(fill)) for n,v,s,fill in zip(self.columns,co,sc.scale_,im.statistics_)]
        rows.append(dict(feature='INTERCEPT',standardized_coefficient=float(np.ravel(np.asarray(est.intercept_))[0]),coefficient=float(np.ravel(np.asarray(est.intercept_))[0]-np.sum(co*sc.mean_/sc.scale_)),impute_value=None));return rows

class ZeroResidual(LinearPlugin):
    def fit(self,X,y,sample_weight=None):
        self.columns=list(X);self.constant=0.;self.meta={};return self

def create(name,alpha,classification=False):
    if name=='residual_zero':return ZeroResidual(alpha,classification)
    if ':' in name:
        module,cls=name.split(':',1);return getattr(importlib.import_module(module),cls)(alpha=alpha,classification=classification)
    if name not in ['baseline_ridge','price_residual_ridge','logistic_with_baseline_selection']:raise ValueError('未知模型插件：'+name)
    return LinearPlugin(alpha,classification)

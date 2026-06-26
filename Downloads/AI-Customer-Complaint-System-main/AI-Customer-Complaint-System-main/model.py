import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

df = pd.read_csv("data.csv")

X = df["ticket"]
y = df["category"]

vectorizer = TfidfVectorizer()

X_vec = vectorizer.fit_transform(X)

model = LogisticRegression()

model.fit(X_vec,y)

def predict_ticket(text):
    vec = vectorizer.transform([text])
    result = model.predict(vec)
    return result[0]
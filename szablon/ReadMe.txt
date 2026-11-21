W katalogu tym znajdują się wszystkie pliki szablonu w języku Latex prac dyplomowych dla studentów WIT PWr.
Szablon jest dopasowany do kompilatora pdflatex.
Data : 25.02.2026
Autor: Komisja d/s Jakośći Kształcenia WIT PWr

===================================

Zawartość katalogu
  - main.tex: główny plik 
  - settings.tex: plik w którym ustawione są główne parametry szablonu (marginesy, czcionki, ...) oraz podstawowe makra (np. makro do budowania strony tytułowej) 
  - abstract.tex: plik w którym należy wpisać abstrakty pracy w języku polskim i angielskim 
  - bibliography.bib: plik z przykładową bibliografią 
  - acronyms.tex: plik z przykładowymi akronimami (skrótami) 
  -[chapters]: katalog do umieszczania swoich rozdziałów
     * wstep.tex: przykładowy wstęp do pracy z komentarzami
     * info.tex: plik z informacjami o tym pakiecie
  -[figures]: katalog do umieszczania swoich ilustracji
  
====================================

UWAGI: 

1. W szbalonie stosujemy kilka dosyć nowych narzędzi Latex'owych, np. clevref, acronyms.
2. Korzystamy również z programu biber i pakietu biblatex. Jeśli bardzo musisz, to możesz to zamienić na stare rozwiązanie korzystające z bibtex'a (musisz tylko zmienić kilka linijek w pliku main.tex).
3. Jeśli korzystsz ze środowiska overleaf, to ono samo powinno wykryś stosowanie bibera.
4. Jeśli korzystasz z innych środowisk, to musisz samemu, w parametrach kompilacji, wskazać, że korzystasz z bibera. Jeśli masz z tym problem, to możesz wywołać program biber z linijki poleceń z parametrem którym jest nazwa głównego pliku bez rozszerzenia, czyli w naszym przypadku "biber main".
5. Po kilkukrotnym skomplilowaniu pliku main.tex (przed wprowadzniem swoich modyfikacji) powinien Ci się wyświetlić komunikat: 
"LaTeX-Result: 0 Error(s), 0 Warning(s), 0 Bad Box(es), 12 Pages

     

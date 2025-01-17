#!/usr/bin/fish
#                   1                   2                     3                4            5               6           7             8                9          10           11           12                   13           14             15                    16           17             18            19              20           21            22             23                  24                    25    
set -g subjects     alexachung          alita                 caradelevingne   donnieyen    gabrielleunion iainarmitage jaychou      jenniferlawrence  jiffpom    keanureeves  lilbub       lisa                 masatosakai  michelleyeoh   princessmonstertruck  ryangosling  sandraoh       selenagomez   smritimandhana  spikelee     stephenchow   taylorswift    timotheechalamet    tomholland            zendaya
set -g db_prompts   "young woman"       "cyborg young girl"   "young woman"    "asian man"  "black woman"  "young boy"  "asian man"  "young woman"     "pom dog"  "white man"  "tabby cat"  "asian young woman"  "asian man"  "asian woman"  "black persian cat"   "white man"  "asian woman"  "young girl"  "indian girl"   "black man"  "asian man"   "young woman"  "white young man"   "white young man"     "young girl"
set -g cls_tokens   girl                girl                  girl             man          woman          boy          man          girl              dog        man          cat          girl                 man          woman          cat                   man          woman          girl          girl            man          man           girl           man                 man                   girl
set -g ada_prompts  $db_prompts
set -g ada_weights  "1 2"               "1 1 2"               "1 2"            "1 2"        "1 2"          "1 2"        "1 2"        "1 2"             "1 1"      "1 2"        "1 2"        "1 1 2"              "1 2"        "1 2"          "1 1 3"               "1 2"        "1 2"          "1 2"         "1 2"           "1 2"        "1 2"         "1 2"          "1 1 2"             "1 1 2"               "1 2"
# broad_classes are all 1, i.e., humans/animals.
set -g broad_classes  1                 1                     1                1            1              1            1            1                 1          1            1            1                    1            1              1                     1            1              1             1               1            1             1              1                   1                     1           
# Most subjects are human faces, except for the 3 cats/dogs. $are_faces instructs the generation script 
# whether to compute face similarity.
# $are_faces are used only for evaluation, not for training.
set -g are_faces     1                  1                     1                1            1              1            1            1                 0          1            0            1                    1            1              0                     1            1              1             1               1            1             1              1                   1                     1
# $use_fp_trick is by default enabled (value 1), but disabled for difficult subjects (dominating compositions).
# set -g use_fp_trick  1                1                     0                1            1              1            1            0                 1          0            1            1                    1            1              1                     1            1              1             1               1            1             0              0                   1                     1
# -1: use default range [0.1, 0.3]. 0.6: hard subjects, use [0.2, 0.6].
# set -g prompt_mix_max  -1               -1                    0.9              -1           -1             -1           -1           0.9               -1         0.9          -1           -1                   -1           -1             -1                    0.6          -1             0.6           -1              -1           -1            1.2            0.9                 -1                    -1
#                     objects    humans/animals    cartoon characters
set -g lrs            3e-4       8e-4              3e-4
set -g z_prefixes     ""         "portrait of"     ""
set -g maxiters       3500       4500              3500
# All subjects are humans/animals. The other two classes are listed for completeness.
# Individual LR for each class in the broad classes, according to their difficulties / inherent complexity.
# A prefix of "portrait of" for animals/humans suggests SD to focus on the face area of the subject.

# donnieyen jenniferlawrence jiffpom lilbub lisa michelleyeoh selenagomez smitrimandhana taylorswift zendaya
set -g sel_set              4 8 9 11 12 14 18 19 22 25
# "instagram" for the main dataset, to focus on faces.
set -g db_suffix           ", instagram"
set -g data_folder         data
